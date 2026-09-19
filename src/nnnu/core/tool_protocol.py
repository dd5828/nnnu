"""工具协议（§6.3）：Level 1 插件——单函数工具，LLM 自主决定调用。

挂载规则（ToolMountFlags + compute_mounted_tools，纯函数）：
- user_toggleable：用户在设置/工具栏开关（默认不挂载）；
- context_gated：回合上下文条件命中时自动挂载（flags.context 由编排器计算）；
- always：恒挂载；
- forced 无条件追加（绕过所有门），suppressed 无条件移除；
- 任何门控条件必须 fail-closed（异常视为不命中），forced 未知名忽略并告警。
"""

import logging
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar, Iterable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ToolMount(StrEnum):
    USER_TOGGLEABLE = "user_toggleable"
    CONTEXT_GATED = "context_gated"
    ALWAYS = "always"


class ToolDefinition(BaseModel):
    name: str  # 全局唯一，如 "web_search"
    description: str  # 供模型阅读的能力描述（双语提示词键，如 "tools.ask_user"）
    parameters: dict[str, Any] = Field(default_factory=dict)  # JSON Schema
    mount: ToolMount = ToolMount.USER_TOGGLEABLE
    cost_hint: str | None = None  # 如 "may cost tokens/time"

    def to_openai_schema(self) -> dict[str, Any]:
        """OpenAI function calling 格式（§6.6 每轮注入消息历史的工具定义）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolResult(BaseModel):
    ok: bool
    output: str  # 给模型看的文本
    detail: dict[str, Any] | None = None  # 给前端展示的结构化数据（如引用列表）
    usage: dict[str, Any] | None = None  # tokens/耗时


class ToolContext(BaseModel):
    """工具执行上下文：模型输入之外的一切从 ctx 取（不污染 LLM 可见参数面）。"""

    turn_id: str
    session_id: str
    language: str = "zh"
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)  # 运行期通道（如 ask_user_fn）


class BaseTool(ABC):
    definition: ClassVar[ToolDefinition]

    @abstractmethod
    async def run(self, ctx: ToolContext) -> ToolResult: ...


class ToolMountFlags(BaseModel):
    """回合级挂载修饰（§6.3）：编排器按会话偏好/请求计算。"""

    context: set[str] = Field(default_factory=set)  # 上下文门条件命中的工具
    forced: set[str] = Field(default_factory=set)  # 强制启用（等价 --tool）
    suppressed: set[str] = Field(default_factory=set)


def compute_mounted_tools(tools: Iterable[ToolDefinition], flags: ToolMountFlags) -> list[str]:
    """挂载纯函数：(always ∪ context_gated∧命中 ∪ forced) − suppressed。

    forced 未知名忽略并告警（不产生幻觉工具）；suppressed 优先于 forced。
    """
    definitions = list(tools)
    known = {d.name for d in definitions}
    mounted: list[str] = []
    for definition in definitions:
        if definition.name in flags.suppressed:
            continue
        if definition.mount == ToolMount.ALWAYS:
            mounted.append(definition.name)
        elif definition.mount == ToolMount.CONTEXT_GATED and definition.name in flags.context:
            mounted.append(definition.name)
    for name in flags.forced:
        if name in flags.suppressed:
            continue
        if name in known and name not in mounted:
            mounted.append(name)
        elif name not in known:
            logger.warning("forced 工具 %s 未注册，已忽略", name)
    return mounted
