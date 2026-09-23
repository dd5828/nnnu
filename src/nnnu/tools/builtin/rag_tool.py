"""rag 工具（§7.9）：用户在会话里选了知识库时挂载的 context_gated 工具。

检索范围只限用户选中的库，且 kb_name 必填（对齐上游 DeepTutor：没选库就不挂、
也不许默认全库检索）。库名 → kb_id 的映射由 chat 能力经 ctx.metadata["rag_kbs"]
注入；kb_name 缺失或不在映射里直接报错，绝不猜默认值。

输出给模型的是带来源标注的逐行片段（库名 + 文件名 + 页码），
detail.sources 对齐 CitationSource（doc_id/kb/page/snippet），
走 agent_loop 既有通路 → done.citations → 前端引用面板。
"""

from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

SNIPPET_MAX_CHARS = 300
MAX_TOP_K = 10
SEARCH_MODES = ("auto", "vector", "hybrid")


class RagSearchTool(BaseTool):
    definition = ToolDefinition(
        name="rag",
        description="tools.rag",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题或关键词"},
                "kb_name": {
                    "type": "string",
                    "description": "要检索的知识库名称，必填，只能从用户已挂载的库里选",
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K, "default": 5},
                "mode": {
                    "type": "string",
                    "enum": list(SEARCH_MODES),
                    "default": "auto",
                    "description": "auto 混合检索；vector 只走向量；hybrid 向量+BM25",
                },
            },
            "required": ["query", "kb_name"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.embedding.base import EmbeddingError
        from nnnu.services.knowledge.service import get_kb_service

        try:
            service = get_kb_service()
        except RuntimeError:
            return ToolResult(ok=False, output="知识库服务没装配，暂时用不了。")

        query = str(ctx.args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, output="query 不能为空。")
        mounted: dict[str, str] = ctx.metadata.get("rag_kbs") or {}
        available = "、".join(mounted) or "无"
        kb_name = str(ctx.args.get("kb_name", "")).strip()
        if not kb_name:
            return ToolResult(
                ok=False,
                output=f"kb_name 必填：请从用户已挂载的知识库里选一个（已挂载：{available}）。",
            )
        kb_id = mounted.get(kb_name)
        if kb_id is None:
            return ToolResult(
                ok=False,
                output=f"知识库「{kb_name}」不在已挂载列表里（已挂载：{available}）。",
            )
        top_k = max(1, min(int(ctx.args.get("top_k", 5)), MAX_TOP_K))
        mode = str(ctx.args.get("mode", "auto"))
        if mode not in SEARCH_MODES:
            mode = "auto"

        try:
            hits = await service.search(kb_id, query, mode=mode, top_k=top_k)
        except EmbeddingError as exc:
            return ToolResult(ok=False, output=f"知识库检索失败：嵌入端点不可用（{exc}）。")

        if not hits:
            return ToolResult(
                ok=True, output=f"知识库「{kb_name}」中没有检索到与「{query}」相关的内容。"
            )

        lines = [f"知识库检索命中 {len(hits)} 段（引用时标注出处与页码）："]
        sources: list[dict[str, Any]] = []
        for hit in hits:
            snippet = hit.text[:SNIPPET_MAX_CHARS]
            kb_name = str(hit.metadata.get("kb_name") or hit.kb_id)
            filename = str(hit.metadata.get("filename") or hit.doc_id)
            where = f"第 {hit.page} 页" if hit.page else "无页码"
            lines.append(f"- 《{kb_name}》{filename} {where}：{snippet}")
            sources.append(
                {
                    "doc_id": hit.doc_id,
                    "kb": hit.kb_id,
                    "page": hit.page,
                    "snippet": snippet,
                }
            )
        return ToolResult(ok=True, output="\n".join(lines), detail={"sources": sources})
