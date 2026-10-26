"""检索引擎抽象与公共类型（§7.9）。

P4 只实现 vector（自研：numpy 余弦 + BM25 + RRF 融合，偏离方案原定的
llama-index，理由见 ARCHITECTURE 决策 #16）；P14 补上 pageindex（内置页级
检索）与 graphrag / lightrag（可选重依赖，装 `rag-graph` extra 后可用）。

`needs_embeddings` 是给 KBService 的分工开关：vector / pageindex 自己吃
chunks+向量；graphrag / lightrag 在引擎内部自己调模型，外层就别再嵌入一遍
（省一次全量嵌入，也让图谱库的构建时间只花在它真正需要的地方）。

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


class EngineUnavailable(RuntimeError):
    """引擎的选装依赖没装/配置不全（消息写给用户看，直接进界面）。"""


class BaseEngine(ABC):
    """索引引擎契约：写版本目录 + 从版本目录检索。"""

    engine_id: str = ""
    #: 外层管线要不要先算 chunks+向量。graph 引擎自己嵌入，设 False。
    needs_embeddings: bool = True
    #: 这个引擎原生支持的检索模式（界面提示用；未知模式由引擎自己回退）
    supported_modes: tuple[str, ...] = ("vector", "hybrid")

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

    def invalidate(self, index_dir: Path) -> None:
        """丢掉这个版本目录的进程内缓存（目录被删/被覆盖后调用；默认无缓存）。"""
