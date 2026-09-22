"""vector 引擎（§7.9）：numpy 余弦 + BM25 + RRF 混合。

版本目录里的三件套：
- chunks.jsonl   每行一个块（chunk_id/doc_id/seq/text/page），行序 = npy 行序
- embeddings.npy float32 [n, dim]，已 L2 归一化（点积即余弦）
- bm25.json      词法索引（bm25.Bm25Index.to_json）

meta.json 不在这里写：它是版本「就绪」的标志，由 KBService 最后落笔，
保证「active_version 指向的目录一定是完整的」。

检索模式（mode）：
- vector：查询向量与矩阵点积，取 top_k×2 候选，score = 余弦相似度
- hybrid：向量与 BM25 各取 top_k×2 → RRF(k=60) 融合取 top_k，score = RRF 分
两种模式的 score 尺度不同，只用于各自内部排序（跨 KB 合并由上层再 RRF）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from nnnu.services.rag.base import BaseEngine, Chunk, Hit
from nnnu.services.rag.bm25 import Bm25Index
from nnnu.services.rag.rrf import rrf_fuse

if TYPE_CHECKING:
    from nnnu.services.embedding.base import EmbeddingProvider

CHUNKS_FILE = "chunks.jsonl"
VECTORS_FILE = "embeddings.npy"
BM25_FILE = "bm25.json"
META_FILE = "meta.json"

CANDIDATE_MULTIPLIER = 2  # 两路各取 top_k×2 再融合，防止好结果被截断


class IndexDimMismatchError(ValueError):
    """查询向量的维度与索引不一致（换了嵌入模型/端点，需要重建索引）。"""


class LoadedIndex:
    """加载后的版本索引（chunks 与 vectors 行序一一对应）。"""

    def __init__(
        self,
        chunks: list[Chunk],
        vectors: np.ndarray,
        bm25: Bm25Index,
    ) -> None:
        self.chunks = chunks
        self.vectors = vectors
        self.bm25 = bm25
        self.by_id = {chunk.chunk_id: chunk for chunk in chunks}

    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1]) if self.vectors.size else 0


class VectorEngine(BaseEngine):
    engine_id = "vector"

    def __init__(self) -> None:
        # 进程内缓存：index_dir → (mtime 令牌, 索引)。活跃版本切换后令牌变化自然失效。
        self._cache: dict[Path, tuple[int, LoadedIndex]] = {}

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
        """整目录重写（增量新增也走这里：上层合并好全量再调）。"""
        index_dir.mkdir(parents=True, exist_ok=True)
        self._write_chunks(index_dir / CHUNKS_FILE, chunks)
        self._write_vectors(index_dir / VECTORS_FILE, vectors)
        self._write_json(index_dir / BM25_FILE, Bm25Index.from_chunks(chunks).to_json())
        self._cache.pop(index_dir, None)

    @staticmethod
    def _write_chunks(path: Path, chunks: list[Chunk]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for chunk in chunks:
                handle.write(json.dumps(chunk.model_dump(), ensure_ascii=False) + "\n")
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
        """加载版本索引（带 mtime 缓存：重建/增量后自动失效）。"""
        token = (index_dir / VECTORS_FILE).stat().st_mtime_ns
        cached = self._cache.get(index_dir)
        if cached and cached[0] == token:
            return cached[1]
        chunks = self.read_chunks(index_dir)
        vectors = np.load(index_dir / VECTORS_FILE)
        bm25 = Bm25Index.from_json(json.loads((index_dir / BM25_FILE).read_text(encoding="utf-8")))
        loaded = LoadedIndex(chunks, vectors, bm25)
        self._cache[index_dir] = (token, loaded)
        return loaded

    @staticmethod
    def read_chunks(index_dir: Path) -> list[Chunk]:
        """读 chunks.jsonl（增量新增时上层要拿旧块合并）。"""
        path = index_dir / CHUNKS_FILE
        if not path.exists():
            return []
        chunks: list[Chunk] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    chunks.append(Chunk.model_validate(json.loads(line)))
        return chunks

    def invalidate(self, index_dir: Path) -> None:
        self._cache.pop(index_dir, None)

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
        loaded = self.load(index_dir)
        if not loaded.chunks:
            return []
        keep = self._keep_mask(loaded, excluded_docs)
        ranked, scores = await self._vector_rank(loaded, query, keep, top_k, embedder)
        if mode == "vector":
            return self._to_hits(loaded, kb_id, ranked[:top_k], scores)
        lexical = loaded.bm25.search(
            query, top_k * CANDIDATE_MULTIPLIER, excluded_docs=excluded_docs
        )
        lexical_ids = [chunk_id for chunk_id, _ in lexical]
        # 词法名次放前面：RRF 只吃名次，两路各有一个第一名时会并列同分，
        # 同分该让「字面确实出现」压过「语义相近」——查错误码、专有名词全靠这个
        fused = rrf_fuse([lexical_ids, ranked])
        # 命中分是 RRF 融合分（不是余弦，量级 ~1/61），查询侧展示与排序都用它
        return self._to_hits(
            loaded,
            kb_id,
            [chunk_id for chunk_id, _ in fused[:top_k]],
            dict(fused),
            lexical_ranks={chunk_id: rank for rank, chunk_id in enumerate(lexical_ids, start=1)},
        )

    async def _vector_rank(
        self,
        loaded: LoadedIndex,
        query: str,
        keep: np.ndarray,
        top_k: int,
        embedder: "EmbeddingProvider | None",
    ) -> tuple[list[str], dict[str, float]]:
        """向量召回：返回 (top_k×2 的 chunk_id 列表, chunk_id → 余弦分)。"""
        if embedder is None or not loaded.chunks:
            return [], {}
        vectors = await embedder.embed([query])
        if not vectors:
            return [], {}
        query_vector = np.asarray(vectors[0], dtype=np.float32)
        if query_vector.shape[0] != loaded.dim:
            raise IndexDimMismatchError(
                f"查询向量维度 {query_vector.shape[0]} 与索引维度 {loaded.dim} 不一致，请重建索引。"
            )
        scores = loaded.vectors @ query_vector
        scores = np.where(keep, scores, -np.inf)
        limit = min(top_k * CANDIDATE_MULTIPLIER, len(loaded.chunks))
        # 先 argpartition 取前 limit（O(n)），再对这 limit 个精确排序
        order = (
            np.argpartition(-scores, limit - 1)[:limit]
            if limit < len(scores)
            else np.arange(len(scores))
        )
        order = order[np.argsort(-scores[order], kind="stable")]
        ranked: list[str] = []
        score_map: dict[str, float] = {}
        for index in order:
            score = float(scores[int(index)])
            if not np.isfinite(score):
                continue
            chunk_id = loaded.chunks[int(index)].chunk_id
            ranked.append(chunk_id)
            score_map[chunk_id] = score
        return ranked, score_map

    @staticmethod
    def _keep_mask(loaded: LoadedIndex, excluded_docs: frozenset[str]) -> np.ndarray:
        if not excluded_docs:
            return np.ones(len(loaded.chunks), dtype=bool)
        return np.array([chunk.doc_id not in excluded_docs for chunk in loaded.chunks], dtype=bool)

    @staticmethod
    def _to_hits(
        loaded: LoadedIndex,
        kb_id: str,
        ranked: list[str],
        scores: dict[str, float] | None,
        *,
        lexical_ranks: dict[str, int] | None = None,
    ) -> list[Hit]:
        score_map = scores or {}
        rank_map = lexical_ranks or {}
        hits: list[Hit] = []
        for chunk_id in ranked:
            chunk = loaded.by_id.get(chunk_id)
            if chunk is None:
                continue
            metadata: dict[str, Any] = {"chunk_id": chunk.chunk_id}  # 跨 KB RRF 去重靠它
            if chunk_id in rank_map:
                # 词法名次：跨 KB 融合同分时，有字面命中的库排前面（见 search_all_ready）
                metadata["lexical_rank"] = rank_map[chunk_id]
            hits.append(
                Hit(
                    doc_id=chunk.doc_id,
                    kb_id=kb_id,
                    score=score_map.get(chunk_id, 0.0),
                    text=chunk.text,
                    page=chunk.page,
                    metadata=metadata,
                )
            )
        return hits
