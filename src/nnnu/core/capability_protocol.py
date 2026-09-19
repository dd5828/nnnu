"""能力协议（§6.4）：Level 2 插件——多阶段流水线，接管整个回合。

BaseCapability.run 契约：开始 emit status（每阶段开始）→ 内容/工具事件 →
content_done → cost_summary → done/error；编排器按 bus.terminal_emitted 兜底
（§6.5），能力漏发终局事件不会被静默吞掉。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from nnnu.core.stream_bus import StreamBus

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext


class Stage(BaseModel):
    key: str  # 如 "planning"
    label_i18n: str  # 提示词 YAML 中的键
    max_rounds: int = 20  # 该阶段 Agent 循环最大轮数
    max_tokens: int = 4096  # 该阶段输出上限


class CapabilityManifest(BaseModel):
    name: str  # "chat" | "deep_solve" | ...
    version: str
    stages: list[Stage] = Field(default_factory=list)  # 空 = 单阶段自由循环（chat）
    config_schema: dict[str, Any] = Field(default_factory=dict)  # 该能力的可配置参数
    default_model_role: str = "chat"  # 默认模型角色（普通/推理/视觉…）


class BaseCapability(ABC):
    manifest: ClassVar[CapabilityManifest]

    @abstractmethod
    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        """接管回合：内部必须 emit status（每阶段开始）→ … → cost_summary → done/error。"""
        raise NotImplementedError
