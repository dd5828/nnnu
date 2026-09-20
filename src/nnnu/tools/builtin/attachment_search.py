"""attachment_search 工具（§7.1 附件引用）：有附件时自动挂载的 context_gated 工具。

在回合附件的解析页上做轻量 BM25 检索（中文按单字、ASCII 按词分词）；
命中经 detail.sources 转 citation 事件（agent_loop 既有机制，P4 rag 复用同一通路），
页码定位到 PDF 原页（验收②）。
"""

import math
from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

BM25_K1 = 1.5
BM25_B = 0.75
SNIPPET_MAX_CHARS = 300


def _tokenize(text: str) -> list[str]:
    """中文按单字、ASCII 按字母数字词切分（轻量索引，够 P2 附件规模）。"""
    tokens: list[str] = []
    buffer = ""
    for ch in text.lower():
        if ch.isascii() and ch.isalnum():
            buffer += ch
            continue
        if buffer:
            tokens.append(buffer)
            buffer = ""
        if ch.isalnum():  # 非 ASCII 字母数字（中文等）：单字 token
            tokens.append(ch)
    if buffer:
        tokens.append(buffer)
    return tokens


def _bm25_search(query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    """docs: [{att_id, name, page, text}] → 按 BM25 排序取 top_k（含原始字段）。"""
    doc_tokens = [_tokenize(doc["text"]) for doc in docs]
    lengths = [len(tokens) for tokens in doc_tokens]
    avg_dl = sum(lengths) / len(docs) if docs else 0.0
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    query_terms = [t for t in _tokenize(query) if t in df]
    if not query_terms:
        return []

    n_docs = len(docs)
    scored: list[tuple[float, int]] = []
    for index, tokens in enumerate(doc_tokens):
        score = 0.0
        for term in query_terms:
            tf = tokens.count(term)
            if not tf:
                continue
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * lengths[index] / max(avg_dl, 1.0))
            score += idf * tf * (BM25_K1 + 1) / denom
        if score > 0:
            scored.append((score, index))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [docs[index] for _, index in scored[:top_k]]


class AttachmentSearchTool(BaseTool):
    definition = ToolDefinition(
        name="attachment_search",
        description="tools.attachment_search",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或问题"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            },
            "required": ["query"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        index: list[dict[str, Any]] = ctx.metadata.get("attachment_index", [])
        if not index:
            return ToolResult(ok=False, output="本回合没有可检索的附件。")

        query = str(ctx.args.get("query", ""))
        top_k = min(int(ctx.args.get("top_k", 3)), 10)
        docs: list[dict[str, Any]] = []
        for attachment in index:
            for page in attachment.get("pages", []):
                docs.append(
                    {
                        "att_id": attachment["id"],
                        "name": attachment["name"],
                        "page": page["page"],
                        "text": page["text"],
                    }
                )
        hits = _bm25_search(query, docs, top_k)
        if not hits:
            return ToolResult(ok=True, output=f"附件中未检索到与「{query}」相关的段落。")

        lines = [f"附件检索命中 {len(hits)} 段："]
        sources: list[dict[str, Any]] = []
        for hit in hits:
            snippet = hit["text"][:SNIPPET_MAX_CHARS]
            lines.append(f"- 《{hit['name']}》第 {hit['page']} 页：{snippet}")
            sources.append(
                {
                    "doc_id": hit["att_id"],
                    "kb": "attachment",
                    "page": hit["page"],
                    "snippet": snippet,
                }
            )
        return ToolResult(ok=True, output="\n".join(lines), detail={"sources": sources})
