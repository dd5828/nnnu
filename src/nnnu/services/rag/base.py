"""检索引擎抽象与公共类型（§7.9）。

P4 只实现 vector（自研：numpy 余弦 + BM25 + RRF 融合，偏离方案原定的
llama-index，理由见 ARCHITECTURE 决策 #16）；graphrag / lightrag / pageindex
三个 engine_id 归 P14。

引擎与 KB 生命周期的分工：KBService 负责解析/切块/嵌入的管线与状态机，
引擎只负责「把 chunks+向量写成版本目录」与「从版本目录检索」两件事——
这样版本切换（写新目录、meta.json ready 后原子切指针）是文件系统层面的事，
引擎实现不需要感知状态机。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import numpy as np

    from nnnu.services.embedding.base import EmbeddingProvider


class Chunk(BaseModel):
    """切块结果：doc_id 定位文档，page 定位页码（非 PDF 为 None）。"""

    chunk_id: str
    doc_id: str
    seq: int
    text: str
    page: int | None = None


class Hit(BaseModel):
    """检索命中（§7.9）。score 引擎内可比，跨引擎/跨 KB 不可比。"""

    doc_id: str
    kb_id: str
    score: float
    text: str
    page: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseEngine(ABC):
    """索引引擎契约：写版本目录 + 从版本目录检索。"""

    engine_id: str = ""

    @abstractmethod
    async def write_index(
        self,
        index_dir: Path,
        *,
        chunks: list[Chunk],
        vectors: "np.ndarray",
        dim: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """把切块与向量写进 index_dir（不含 meta.json——ready 标志由调用方最后写）。"""

    @abstractmethod
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
        """检索：mode ∈ {vector, hybrid}；excluded_docs 命中的块一律剔除（删除即时生效）。"""
