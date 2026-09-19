"""ScriptedLLM 测试替身（§12.1）：预置回复序列模拟流式与多轮工具调用。

- 语义：每次 LLM 调用弹出一步（步序即调用序）；
- 流式顺序：thinking 块 → 正文块（≥64 字符合并）→ usage → finish_reason；
- calls 审计列表记录完整 LLMRequest（断言工具定义/参数/多轮历史）；
- 脚本耗尽抛 RuntimeError（防测试假绿——静默空回复是最坏的假通过）；
- from_yaml 载入夹具脚本（tests/fixtures/scripts/*.yaml）。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import yaml

from nnnu.services.llm.protocol import LLMChunk, LLMRequest, LLMResponse, LLMToolCall

logger = logging.getLogger(__name__)

SCRIPTED_CHUNK_CHARS = 64  # 与真实客户端块合并阈值一致，流行为对齐


@dataclass(slots=True)
class ScriptedStep:
    """一次 LLM 调用的脚本：思考 → 正文 → 工具调用 → 结束原因。"""

    chunks: list[str] = field(default_factory=list)  # 正文流式块序列
    thinking: list[str] = field(default_factory=list)  # 思考块（先于正文输出）
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


class ScriptedLLM:
    def __init__(self, steps: list[ScriptedStep]) -> None:
        self._steps = list(steps)
        self.calls: list[LLMRequest] = []

    @property
    def exhausted(self) -> bool:
        return not self._steps

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """流式聚合返回（与真实客户端 complete 语义一致）。"""
        response = LLMResponse()
        async for chunk in self.stream(request):
            response.text += chunk.text
            response.thinking += chunk.thinking
            if chunk.tool_call_delta is not None:
                delta = chunk.tool_call_delta
                index = delta["index"]
                while len(response.tool_calls) <= index:
                    response.tool_calls.append(LLMToolCall(id="", name="", arguments=""))
                call = response.tool_calls[index]
                if delta.get("id"):
                    call.id = delta["id"]
                if delta.get("name"):
                    call.name = delta["name"]
                call.arguments += delta.get("arguments_piece", "")
            if chunk.usage:
                response.usage = chunk.usage
            if chunk.finish_reason:
                response.finish_reason = chunk.finish_reason
        return response

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        self.calls.append(request)
        if not self._steps:
            raise RuntimeError("脚本耗尽：LLM 调用次数超出脚本定义（防测试假绿，请补脚本步骤）")
        step = self._steps.pop(0)

        for thinking_piece in step.thinking:
            yield LLMChunk(thinking=thinking_piece)
        # 工具调用在正文流开始前一次性给出（OpenAI 语义：text 与 tool_calls 不混流）
        for index, call in enumerate(step.tool_calls):
            yield LLMChunk(
                tool_call_delta={
                    "index": index,
                    "id": call.id,
                    "name": call.name,
                    "arguments_piece": call.arguments,
                }
            )
        # 正文按 ≥64 字符合并（对齐真实客户端），小尾巴单独一块
        buffer = ""
        for piece in step.chunks:
            buffer += piece
            while len(buffer) >= SCRIPTED_CHUNK_CHARS:
                yield LLMChunk(text=buffer[:SCRIPTED_CHUNK_CHARS])
                buffer = buffer[SCRIPTED_CHUNK_CHARS:]
        if buffer:
            yield LLMChunk(text=buffer)
        if step.usage:
            yield LLMChunk(usage=step.usage)
        yield LLMChunk(finish_reason=step.finish_reason)

    @classmethod
    def from_yaml(cls, path: Path) -> "ScriptedLLM":
        """载入 YAML 脚本：

        steps:
          - thinking: [..]
            chunks: [..]
            tool_calls: [{id: call-1, name: add, arguments: '{"a":1}'}]
            finish_reason: stop
            usage: {prompt_tokens: 10, completion_tokens: 5}
        """
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        steps = []
        for item in raw.get("steps", []):
            tool_calls = [
                LLMToolCall(
                    id=call.get("id", f"call-{i}"),
                    name=call["name"],
                    arguments=call.get("arguments", "{}"),
                )
                for i, call in enumerate(item.get("tool_calls", []))
            ]
            steps.append(
                ScriptedStep(
                    chunks=list(item.get("chunks", [])),
                    thinking=list(item.get("thinking", [])),
                    tool_calls=tool_calls,
                    finish_reason=item.get("finish_reason", "stop"),
                    usage=dict(item.get("usage", {})),
                )
            )
        return cls(steps)
