"""pageindex 引擎（§7.9，P14 内置简化版）：先按页粗筛，再在页内精排。

方案原文把 PageIndex 描述成「页级引用检索（reasoning retrieval）」。这里不依赖
外部 PageIndex 服务，也不额外调 LLM：把每个文档按页聚成一个「页节点」，先用
向量余弦 + 页级 BM25 粗筛出候选页，再按页内块的相关度精排，命中天然带精确页码。
相邻页会给一点加分——跨页的图表/续写段落靠这一下才不会被切掉。

版本目录里的文件：
- chunks.jsonl / embeddings.npy / bm25.json  ← 复用 vector 引擎的三件套
- pages.jsonl                                 ← 页节点（page_id/doc_id/page/text/chunk_ids）
- page_embeddings.npy                         ← 页向量（页内块向量的均值，已归一化）
- page_bm25.json                              ← 页级词法索引（粗筛用）

非 PDF 文档没有页码（page=None），整篇聚成一个页节点；命中照样能引用到文档，
只是前端不会跳页。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from nnnu.services.rag.base import BaseEngine, Chunk, Hit
from nnnu.services.rag.bm25 import Bm25Index
from nnnu.services.rag.pipelines.vector import IndexDimMismatchError, LoadedIndex, VectorEngine
from nnnu.services.rag.rrf import rrf_fuse

if TYPE_CHECKING:
    from nnnu.services.embedding.base import EmbeddingProvider

PAGES_FILE = "pages.jsonl"
PAGE_VECTORS_FILE = "page_embeddings.npy"
PAGE_BM25_FILE = "page_bm25.json"
PAGE_TEXT_MAX_CHARS = 12_000  # 单页参与粗筛的文本上限（超长页截断，正文块仍完整）
PAGE_CANDIDATE_MULTIPLIER = 4  # 粗筛候选 = top_k × 4
CHUNKS_PER_PAGE = 2  # 每个入选页最多贡献几块（top_k 不够时再放宽）
NEIGHBOR_BONUS = 0.03  # 相邻页在粗筛里也很靠前时的加分（跨页段落）


class LoadedPages:
    """加载后的页索引（页记录与 page_embeddings 行序一一对应）。"""

    def __init__(self, pages: list[dict[str, Any]], vectors: np.ndarray, bm25: Bm25Index) -> None:
        self.pages = pages
        self.vectors = vectors
        self.bm25 = bm25
        self.by_id = {str(page["page_id"]): page for page in pages}

    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1]) if self.vectors.size else 0


class PageIndexEngine(BaseEngine):
    engine_id = "pageindex"
    supported_modes = ("vector", "hybrid", "auto")

    def __init__(self, vector: VectorEngine | None = None) -> None:
        self._vector = vector or VectorEngine()
        self._pages_cache: dict[Path, tuple[int, LoadedPages]] = {}

    # ---- 写 ----

    async def write_index(
        self,
        index_dir: Path,
        *,
        chunks: list[Chunk],
        vectors: np.ndarray,
        dim: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await self._vector.write_index(index_dir, chunks=chunks, vectors=vectors, dim=dim)
        pages, page_vectors = self._build_pages(chunks, vectors)
        self._write_pages(index_dir / PAGES_FILE, pages)
        self._write_vectors(index_dir / PAGE_VECTORS_FILE, page_vectors)
        self._write_json(
            index_dir / PAGE_BM25_FILE,
            Bm25Index.from_chunks(
                [
                    Chunk(
                        chunk_id=str(page["page_id"]),
                        doc_id=str(page["doc_id"]),
                        seq=0,
                        text=str(page["text"]),
                        page=page.get("page"),
                    )
                    for page in pages
                ]
            ).to_json(),
        )
        self._pages_cache.pop(index_dir, None)

    @staticmethod
    def _build_pages(
        chunks: list[Chunk], vectors: np.ndarray
    ) -> tuple[list[dict[str, Any]], np.ndarray]:
        """块 → 页节点；页向量取页内块向量均值后归一化。"""
        if not chunks:
            return [], np.zeros((0, 0), dtype=np.float32)
        if vectors.shape[0] != len(chunks):
            raise ValueError(f"块数 {len(chunks)} 与向量行数 {vectors.shape[0]} 不一致")
        groups: dict[tuple[str, int | None], list[int]] = {}
        for index, chunk in enumerate(chunks):
            groups.setdefault((chunk.doc_id, chunk.page), []).append(index)
        # 文档顺序 + 页码顺序（None 排在该文档最后），保证构建结果可复现
        ordered = sorted(
            groups.items(),
            key=lambda item: (item[0][0], item[0][1] is None, item[0][1] or 0),
        )
        pages: list[dict[str, Any]] = []
        page_vectors: list[np.ndarray] = []
        for (doc_id, page), rows in ordered:
            index = rows[0]
            suffix = "all" if page is None else str(page)
            page_id = f"{doc_id}:p{suffix}"
            text = "\n".join(chunks[row].text for row in rows)[:PAGE_TEXT_MAX_CHARS]
            pages.append(
                {
                    "page_id": page_id,
                    "doc_id": doc_id,
                    "page": page,
                    "text": text,
                    "chunk_ids": [chunks[row].chunk_id for row in rows],
                }
            )
            if vectors.size:
                mean = np.asarray(vectors[rows], dtype=np.float32).mean(axis=0)
                norm = float(np.linalg.norm(mean))
                if norm > 0:
                    mean = mean / norm
                page_vectors.append(mean.astype(np.float32, copy=False))
        matrix = np.vstack(page_vectors) if page_vectors else np.zeros((0, 0), dtype=np.float32)
        return pages, matrix

    @staticmethod
    def _write_pages(path: Path, pages: list[dict[str, Any]]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for page in pages:
                handle.write(json.dumps(page, ensure_ascii=False) + "\n")
        tmp.replace(path)

    @staticmethod
    def _write_vectors(path: Path, vectors: np.ndarray) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as handle:
            np.save(handle, vectors.astype(np.float32, copy=False))
        tmp.replace(path)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    # ---- 读 ----

    def load(self, index_dir: Path) -> LoadedIndex:
        """给 KBService 的增量复用用：页索引底下仍是 vector 的三件套。"""
        return self._vector.load(index_dir)

    def load_pages(self, index_dir: Path) -> LoadedPages:
        path = index_dir / PAGES_FILE
        token = path.stat().st_mtime_ns
        cached = self._pages_cache.get(index_dir)
        if cached and cached[0] == token:
            return cached[1]
        pages: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    pages.append(json.loads(line))
        vectors = np.load(index_dir / PAGE_VECTORS_FILE)
        bm25 = Bm25Index.from_json(
            json.loads((index_dir / PAGE_BM25_FILE).read_text(encoding="utf-8"))
        )
        loaded = LoadedPages(pages, vectors, bm25)
        self._pages_cache[index_dir] = (token, loaded)
        return loaded

    def invalidate(self, index_dir: Path) -> None:
        self._pages_cache.pop(index_dir, None)
        # 页索引底下还压着一层 vector 缓存（chunks + 向量矩阵），一起丢
        self._vector.invalidate(index_dir)
        self._vector.invalidate(index_dir)

    # ---- 查 ----

    async def query(
        self,
        index_dir: Path,
        query: str,
        *,
        kb_id: str,
        top_k: int,
        mode: str,
        excluded_docs: frozenset[str] = frozenset(),
        embedder: "EmbeddingProvider | None" = None,
    ) -> list[Hit]:
        if mode == "auto":
            mode = "hybrid"
        loaded = self.load_pages(index_dir)
        if not loaded.pages:
            return []
        chunk_index = self._vector.load(index_dir)
        keep = np.array(
            [str(page["doc_id"]) not in excluded_docs for page in loaded.pages], dtype=bool
        )

        query_vector: np.ndarray | None = None
        page_cosines: dict[str, float] = {}
        chunk_cosines: dict[str, float] = {}
        if embedder is not None:
            vectors = await embedder.embed([query])
            if vectors:
                query_vector = np.asarray(vectors[0], dtype=np.float32)
                if query_vector.shape[0] != loaded.dim:
                    raise IndexDimMismatchError(
                        f"查询向量维度 {query_vector.shape[0]} 与页索引维度 {loaded.dim} "
                        "不一致，请重建索引。"
                    )
                scores = loaded.vectors @ query_vector
                scores = np.where(keep, scores, -np.inf)
                for index in range(len(loaded.pages)):
                    score = float(scores[index])
                    if np.isfinite(score):
                        page_cosines[str(loaded.pages[index]["page_id"])] = score
                if chunk_index.chunks and query_vector.shape[0] == chunk_index.dim:
                    chunk_scores = chunk_index.vectors @ query_vector
                    for index, item in enumerate(chunk_index.chunks):
                        chunk_cosines[item.chunk_id] = float(chunk_scores[index])

        lexical = loaded.bm25.search(
            query,
            max(top_k * PAGE_CANDIDATE_MULTIPLIER, 1),
            excluded_docs=excluded_docs,
        )
        lexical_ids = [page_id for page_id, _ in lexical]
        lexical_ranks = {page_id: rank for rank, page_id in enumerate(lexical_ids, start=1)}
        vector_ids = sorted(page_cosines, key=lambda item: -page_cosines[item])
        if mode == "vector":
            coarse = vector_ids
        else:
            coarse = [
                page_id
                for page_id, _ in rrf_fuse([lexical_ids, vector_ids])
                if page_id in loaded.by_id
            ]
        if not coarse:
            return await self._vector.query(
                index_dir,
                query,
                kb_id=kb_id,
                top_k=top_k,
                mode="vector" if mode == "vector" else "hybrid",
                excluded_docs=excluded_docs,
                embedder=embedder,
            )

        leading = set(coarse[:5])
        fine: list[tuple[float, str]] = []
        for page_id in coarse:
            page = loaded.by_id[page_id]
            score = page_cosines.get(page_id, 0.0)
            rank = lexical_ranks.get(page_id)
            if rank:
                score += 0.15 / (60 + rank)
            neighbor = self._neighbor_bonus(page, leading)
            score += neighbor
            fine.append((score, page_id))
        fine.sort(key=lambda pair: (-pair[0], pair[1]))

        selected = fine[: max(1, top_k)]
        hits: list[Hit] = []
        per_page = max(
            CHUNKS_PER_PAGE,
            math.ceil(max(1, top_k) / max(1, len(selected))),
        )
        for page_rank, (page_score, page_id) in enumerate(selected, start=1):
            page = loaded.by_id[page_id]
            chunk_ids = [str(item) for item in page.get("chunk_ids") or []]
            chunk_ids.sort(key=lambda item: -chunk_cosines.get(item, 0.0))
            for chunk_rank, chunk_id in enumerate(chunk_ids[:per_page], start=1):
                chunk = chunk_index.by_id.get(chunk_id)
                if chunk is None:
                    continue
                cosine = chunk_cosines.get(chunk_id, 0.0)
                hits.append(
                    Hit(
                        doc_id=chunk.doc_id,
                        kb_id=kb_id,
                        score=page_score + cosine * 0.001,
                        text=chunk.text,
                        page=chunk.page,
                        metadata={
                            "chunk_id": chunk.chunk_id,
                            "page_id": page_id,
                            "page_rank": page_rank,
                            "page_score": page_score,
                            "page_cosine": page_cosines.get(page_id, 0.0),
                            "cosine": cosine,
                            "page_chunk_rank": chunk_rank,
                            **(
                                {"lexical_rank": lexical_ranks[page_id]}
                                if page_id in lexical_ranks
                                else {}
                            ),
                            "neighbor_pages": self._neighbor_pages(page, leading),
                        },
                    )
                )
                if len(hits) >= top_k:
                    return hits
        return hits

    @staticmethod
    def _neighbor_pages(page: dict[str, Any], leading: set[str]) -> list[str]:
        number = page.get("page")
        if not isinstance(number, int):
            return []
        doc_id = str(page["doc_id"])
        return [
            f"{doc_id}:p{number + offset}"
            for offset in (-1, 1)
            if number + offset >= 1 and f"{doc_id}:p{number + offset}" in leading
        ]

    @classmethod
    def _neighbor_bonus(cls, page: dict[str, Any], leading: set[str]) -> float:
        return NEIGHBOR_BONUS if cls._neighbor_pages(page, leading) else 0.0
