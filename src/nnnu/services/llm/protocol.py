"""LLM 客户端协议（§6.10 / §12.1）：循环对模型的唯一依赖面。

- 真实实现：services/llm/openai_compat.py（自研 httpx 兼容客户端）；
- 测试替身：services/llm/scripted.py（ScriptedLLM 实现同一协议，
  注入点在 factory.create_client，编排器/循环无感知）；
- LLMResponse.usage 仅记非零 usage 帧（防网关每 chunk 回显全零覆盖真实计数）。
"""

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


@dataclass(slots=True)
class LLMRequest:
    messages: list[dict[str, Any]]
    model: str
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    thinking_extra: dict[str, Any] | None = None  # extra_body 透传（如 deepseek thinking.type）


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串（解析失败由调用方按无效调用处理）


@dataclass(slots=True)
class LLMChunk:
    text: str = ""
    thinking: str = ""
    tool_call_delta: dict[str, Any] | None = None  # {index, id?, name?, arguments_piece}
    usage: dict[str, int] | None = None  # 仅非零帧
    finish_reason: str | None = None


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    thinking: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """非流式一次调用（流式的聚合版）。"""
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        """流式调用：按块产出 text/thinking/tool_call_delta/usage/finish_reason。"""
        ...
