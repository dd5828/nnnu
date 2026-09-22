"""切块（§7.9）：以解析结果的页为锚，页边界是硬边界。

页码精确是**结构保证**而不是约定：PDF 每个页单独切块，chunk 绝不跨页，
所以命中块的 page 字段天然就是该块所在的真实页码（rag 引用定位靠它）。
非 PDF（Word/Markdown/纯文本）无页码语义，page=None。

页内切分走三级：段落 → 句子 → 硬切（尽量落在逗号/空白处），再贪心合并到
chunk_size（按字符计——检索侧只用 numpy 与自写 BM25，不依赖 tokenizer，
字符计简单且确定性）；相邻块之间带 overlap 字符的重叠，避免答案正好被切断。
"""

from __future__ import annotations

import re

from nnnu.services.parsing.service import ParsedDocument
from nnnu.services.rag.base import Chunk

DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 50

# 句子边界：中文标点 + ASCII 标点（分号也算，切得细一点再由合并兜回来）
_SENTENCE_END = "。！？!?；;"
# 硬切时优先找的切点（从右往左找最近的）
_CUT_MARKS = "，,、）)】」》 \n"


def chunk_document(
    parsed: ParsedDocument,
    doc_id: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """解析结果 → 块列表。空页/空文档返回空列表（不报错，由调用方记账）。"""
    chunk_size = max(64, chunk_size)
    overlap = max(0, min(overlap, chunk_size // 2))
    chunks: list[Chunk] = []
    seq = 0
    if parsed.pages:
        for page in parsed.pages:
            for piece in _pack(_segments(page.text, chunk_size), chunk_size, overlap):
                chunks.append(_make(chunk_id=doc_id, seq=seq, text=piece, page=page.page))
                seq += 1
    elif parsed.text.strip():
        for piece in _pack(_segments(parsed.text, chunk_size), chunk_size, overlap):
            chunks.append(_make(chunk_id=doc_id, seq=seq, text=piece, page=None))
            seq += 1
    return chunks


def _make(chunk_id: str, seq: int, text: str, page: int | None) -> Chunk:
    return Chunk(chunk_id=f"{chunk_id}#{seq}", doc_id=chunk_id, seq=seq, text=text, page=page)


def _segments(text: str, limit: int) -> list[str]:
    """段落 → 行 → 句子 → 硬切，产出不超过 limit 的语义片段。"""
    segments: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            line = line.strip()
            if not line:
                continue
            for sentence in _split_sentences(line):
                segments.extend(_hard_split(sentence, limit))
    return segments


def _split_sentences(line: str) -> list[str]:
    """按句末标点切句（标点留在前句尾）。"""
    out: list[str] = []
    buffer = ""
    for ch in line:
        buffer += ch
        if ch in _SENTENCE_END:
            out.append(buffer)
            buffer = ""
    if buffer:
        out.append(buffer)
    return [s for s in (s.strip() for s in out) if s]


def _hard_split(text: str, limit: int) -> list[str]:
    """超长片段切成不超过 limit 的块（优先在标点/空白处下刀）。"""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    while len(text) > limit:
        cut = _best_cut(text, limit)
        pieces.append(text[:cut].strip())
        text = text[cut:]
    if text.strip():
        pieces.append(text.strip())
    return [p for p in pieces if p]


def _best_cut(text: str, limit: int) -> int:
    window = text[:limit]
    for mark in _CUT_MARKS:
        pos = window.rfind(mark)
        if pos >= limit // 2:  # 切点太靠前就宁可硬切，避免产出碎块
            return pos + 1
    return limit


def _pack(segments: list[str], chunk_size: int, overlap: int) -> list[str]:
    """贪心合并片段到 chunk_size；块间保留 overlap 字符的重叠。"""
    chunks: list[str] = []
    buffer = ""
    for segment in segments:
        if not buffer:
            buffer = segment
            continue
        candidate = f"{buffer}\n{segment}"
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue
        chunks.append(buffer)
        tail = buffer[-overlap:].lstrip("\n") if overlap else ""
        keep = min(len(tail), max(0, chunk_size - len(segment) - 1))
        tail = tail[-keep:] if keep else ""
        buffer = f"{tail}\n{segment}" if tail else segment
    if buffer.strip():
        chunks.append(buffer)
    return [c.strip() for c in chunks if c.strip()]
