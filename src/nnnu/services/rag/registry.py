"""检索引擎注册表（§7.9，P14）。

四个 engine_id 的可用性在这里收口，路由和界面都只问这一处：

- vector / pageindex：内置，永远可用；
- lightrag / graphrag：可选重依赖，装了才可用；没装时仍然出现在列表里，
  带一句「执行 pip install 'nnnu[rag-graph]'」的说明——把入口藏掉，用户
  只会以为功能不存在。

注意这里只查「依赖装没装」，不查模型配没配：模型是运行期才需要的东西，
已建好的图谱库查询时也不一定再调 LLM（不同模式不一样）。真缺模型时由引擎
在构建/查询时抛 `EngineUnavailable`，消息同样写给用户看。
"""

from __future__ import annotations

from dataclasses import dataclass

from nnnu.services.rag.base import BaseEngine, EngineUnavailable
from nnnu.services.rag.pipelines.graphrag import GraphRagEngine
from nnnu.services.rag.pipelines.lightrag import LightRagEngine
from nnnu.services.rag.pipelines.pageindex import PageIndexEngine
from nnnu.services.rag.pipelines.vector import VectorEngine

ENGINE_IDS: tuple[str, ...] = ("vector", "pageindex", "lightrag", "graphrag")
DEFAULT_ENGINE = "vector"


@dataclass(frozen=True, slots=True)
class EngineInfo:
    id: str
    available: bool
    reason: str
    needs_embeddings: bool
    modes: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "available": self.available,
            "reason": self.reason,
            "needs_embeddings": self.needs_embeddings,
            "modes": list(self.modes),
        }


def _info(engine: BaseEngine) -> EngineInfo:
    available, reason = engine.available() if hasattr(engine, "available") else (True, "")
    return EngineInfo(
        id=engine.engine_id,
        available=bool(available),
        reason=str(reason or ""),
        needs_embeddings=engine.needs_embeddings,
        modes=engine.supported_modes,
    )


def engine_infos() -> list[EngineInfo]:
    """固定顺序（vector 永远排第一，作为默认）。"""
    return [
        _info(VectorEngine()),
        _info(PageIndexEngine()),
        _info(LightRagEngine()),
        _info(GraphRagEngine()),
    ]


def engine_info(engine_id: str) -> EngineInfo:
    for info in engine_infos():
        if info.id == engine_id:
            return info
    raise EngineUnavailable(f"未知检索引擎 {engine_id!r}：取 {ENGINE_IDS} 之一")


def ensure_engine_available(engine_id: str) -> EngineInfo:
    info = engine_info(engine_id)
    if not info.available:
        raise EngineUnavailable(info.reason or f"检索引擎 {engine_id} 当前不可用")
    return info


def build_engine(engine_id: str) -> BaseEngine:
    ensure_engine_available(engine_id)
    if engine_id == "vector":
        return VectorEngine()
    if engine_id == "pageindex":
        return PageIndexEngine()
    if engine_id == "lightrag":
        return LightRagEngine()
    if engine_id == "graphrag":
        return GraphRagEngine()
    raise EngineUnavailable(f"未知检索引擎 {engine_id!r}：取 {ENGINE_IDS} 之一")
