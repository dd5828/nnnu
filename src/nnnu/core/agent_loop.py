"""Agent 循环状态机（§6.6）：所有能力共用，chat 即单阶段自由循环。

硬性规则实现：
- max_rounds（工具调用轮）默认 20：超限 → 要求模型收尾（一次机会）→ 再超 error；
- 截断续写：finish_reason=length 自动续写（上限 2 次），续写拼接到同一正文流；
- 空工具调用拒绝：无效调用双 strike（重试一次，再犯 error），不进入执行；
- DSML 工具调用解析：正文夹带标记时解析执行，正文不丢（经 DSMLStreamFilter）；
- 每轮把工具定义与结果注入消息历史；
- 预算裁剪：超限按"最旧工具结果 → 最旧消息"顺序裁剪，保留 system 与最近 3 轮；
- ask_user 暂停/恢复：工具执行内阻塞（AskUserFn 由传输层注入）；
- 取消：CancelledError → emit stopped → re-raise（收尾归 TurnRuntime）；
- 成本：usage 帧记入 ctx.cost，无帧回退字符估算。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nnnu.core.budget import schema_tokens, total_tokens, trim_history
from nnnu.core.dsml import DSMLStreamFilter, extract_dsml_tool_calls
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolResult
from nnnu.services.llm.errors import (
    LLMContextWindowError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from nnnu.services.llm.protocol import LLMClient, LLMRequest, LLMResponse, LLMToolCall

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

MAX_CONTINUATIONS = 2  # 截断续写轮数上限（§6.6"自动续写一轮"+兜底）
TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}


class ToolSet:
    """循环对已挂载工具的唯一视图：名称/schema/执行，全部 fail-closed。"""

    def __init__(
        self, tools: dict[str, BaseTool], descriptions: dict[str, str] | None = None
    ) -> None:
        self._tools = tools
        self._descriptions = descriptions or {}

    def names(self) -> list[str]:
        return list(self._tools)

    def json_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            definition = tool.definition
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": self._descriptions.get(name, definition.description),
                        "parameters": definition.parameters,
                    },
                }
            )
        return schemas

    async def run(self, name: str, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, output=f"未知工具 {name}")
        try:
            return await tool.run(ctx)
        except Exception as exc:
            logger.exception("工具 %s 执行异常", name)
            return ToolResult(ok=False, output=f"工具 {name} 执行异常: {exc}")


AskUserFn = Callable[[str, list[str], str], Awaitable[str]]


@dataclass(slots=True)
class LoopDeps:
    client: LLMClient
    tools: ToolSet
    model: str
    provider: str = ""
    max_rounds: int = 20
    max_output_tokens: int = 4096
    token_budget: int = 32000
    ask_user: AskUserFn | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    thinking_extra: dict[str, Any] | None = None


@dataclass(slots=True)
class LoopOutcome:
    final_text: str = ""
    thinking: str = ""
    completed: bool = True
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)


def _json_or_none(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _as_openai_call(call: LLMToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


async def _call_round(
    deps: LoopDeps, request: LLMRequest, bus: StreamBus, dsml_filter: DSMLStreamFilter
) -> tuple[LLMResponse, str]:
    """流式调用一轮：emit thinking/content 增量，返回 (聚合响应, 原始正文)。

    429 重试仅限尚无任何输出时（已流出内容不可重放，避免重复散文/工具调用）。
    """
    for attempt in range(3):  # 初次 + 2 次重试（§6.10 指数退避）
        response = LLMResponse()
        calls: list[LLMToolCall] = []
        raw_text = ""
        emitted = False
        try:
            async for chunk in deps.client.stream(request):
                emitted = True
                if chunk.thinking:
                    response.thinking += chunk.thinking
                    await bus.emit_thinking_delta(text=chunk.thinking)
                if chunk.text:
                    raw_text += chunk.text
                    clean = dsml_filter.feed(chunk.text)
                    if clean:
                        response.text += clean
                        await bus.emit_content_delta(text=clean)
                if chunk.tool_call_delta is not None:
                    _accumulate_tool_call(calls, chunk.tool_call_delta)
                if chunk.usage:
                    response.usage = chunk.usage
                if chunk.finish_reason:
                    response.finish_reason = chunk.finish_reason
            # 流尾：DSML 残留原样释放（不丢 provider 输出）
            leftover = dsml_filter.flush()
            if leftover:
                response.text += leftover
                await bus.emit_content_delta(text=leftover)
            response.tool_calls = calls
            return response, raw_text
        except LLMRateLimitError as exc:
            if attempt >= 2 or emitted:
                raise
            delay = exc.retry_after if exc.retry_after is not None else float(2**attempt)
            logger.warning("429 限流，%.1fs 后第 %d/2 次重试", delay, attempt + 1)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable：重试循环必然 return 或 raise")  # pragma: no cover


def _accumulate_tool_call(accumulator: list[LLMToolCall], delta: dict[str, Any]) -> None:
    index = int(delta.get("index", 0))
    while len(accumulator) <= index:
        accumulator.append(LLMToolCall(id="", name="", arguments=""))
    call = accumulator[index]
    if delta.get("id"):
        call.id = delta["id"]
    if delta.get("name"):
        call.name = delta["name"]
    call.arguments += delta.get("arguments_piece") or ""


def _invalid_calls(calls: list[LLMToolCall], tool_names: list[str]) -> list[LLMToolCall]:
    """无效调用：空 name / 不在挂载清单 / arguments 不是合法 JSON 对象。"""
    invalid: list[LLMToolCall] = []
    for call in calls:
        if not call.name or call.name not in tool_names:
            invalid.append(call)
        elif call.arguments and _json_or_none(call.arguments) is None:
            invalid.append(call)
    return invalid


async def run_agent_loop(
    ctx: "UnifiedContext",
    bus: StreamBus,
    deps: LoopDeps,
    history: list[dict[str, Any]],
    stop_event: asyncio.Event | None = None,
) -> LoopOutcome:
    """§6.6 状态机：exploring → (tool_call → tool_result)* → responding → done。"""
    outcome = LoopOutcome()
    messages = list(history)
    schemas = deps.tools.json_schemas()
    schema_map = {s["function"]["name"]: s["function"]["parameters"] for s in schemas}
    reserve = schema_tokens(schemas) + deps.max_output_tokens + 500
    tool_rounds = 0
    strikes = 0
    continuations = 0
    finish_requested = False
    context_retried = False
    answer_parts: list[str] = []
    thinking_parts: list[str] = []

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                await bus.emit_stopped()
                outcome.completed = False
                return outcome

            messages, removed = trim_history(
                messages, budget=deps.token_budget, reserve=reserve, keep_last_rounds=3
            )
            if removed:
                await bus.emit_warning(message=f"上下文预算超限，已裁剪 {removed} 条历史消息")

            request = LLMRequest(
                messages=messages,
                model=deps.model,
                tools=schemas,
                temperature=deps.temperature,
                max_tokens=deps.max_output_tokens,
                reasoning_effort=deps.reasoning_effort,
                thinking_extra=deps.thinking_extra,
            )
            try:
                response, raw_text = await _call_round(deps, request, bus, DSMLStreamFilter())
            except LLMContextWindowError as exc:
                if context_retried:
                    await bus.emit_error(message=f"上下文超长: {exc.message}", recoverable=False)
                    outcome.completed = False
                    return outcome
                context_retried = True
                deps.token_budget = int(deps.token_budget * 0.8)
                await bus.emit_warning(message="上下文超长，预算降为 80% 裁剪后重试一次")
                continue
            except LLMTimeoutError as exc:
                # 已流出的正文拼进答案流；有部分输出则续写一次
                if answer_parts and continuations < MAX_CONTINUATIONS:
                    continuations += 1
                    messages.append({"role": "assistant", "content": "".join(answer_parts)})
                    messages.append(
                        {
                            "role": "user",
                            "content": "（继续：上一段因超时中断，请接着写，不要重复已写内容）",
                        }
                    )
                    await bus.emit_warning(message="流式超时，续写一次")
                    continue
                await bus.emit_error(message=f"流式超时: {exc.message}", recoverable=exc.retryable)
                outcome.completed = False
                return outcome
            except LLMError as exc:
                await bus.emit_error(message=exc.message, recoverable=exc.retryable)
                outcome.completed = False
                return outcome

            if response.thinking:
                thinking_parts.append(response.thinking)
                await bus.emit_thinking_done(text=response.thinking)
            _record_cost(ctx, deps, response, messages)

            # ① 截断续写（finish_reason=length）——续写文本拼接到同一正文流
            if response.finish_reason in TRUNCATED_FINISH_REASONS and response.text:
                answer_parts.append(response.text)
                if continuations < MAX_CONTINUATIONS:
                    continuations += 1
                    messages.append({"role": "assistant", "content": response.text})
                    messages.append(
                        {
                            "role": "user",
                            "content": "（继续：上一段被截断，请接着写，不要重复已写内容）",
                        }
                    )
                    continue
                await bus.emit_warning(message="截断续写次数用尽，按已收到内容收尾")
                outcome.final_text = "".join(answer_parts)
                outcome.thinking = "".join(thinking_parts)
                await bus.emit_content_done(full_text=outcome.final_text)
                return outcome

            # ② DSML 解析：原生 tool_calls 优先（避免双派发）
            calls = response.tool_calls
            if not calls:
                dsml_calls, _ = extract_dsml_tool_calls(raw_text, schema_map)
                if dsml_calls:
                    calls = [
                        LLMToolCall(
                            id=call.call_id,
                            name=call.name,
                            arguments=json.dumps(call.arguments, ensure_ascii=False),
                        )
                        for call in dsml_calls
                    ]

            # ③ 无效工具调用拒绝：双 strike（§6.6"判错重试一次，不进入循环"）
            if calls:
                invalid = _invalid_calls(calls, deps.tools.names())
                if invalid:
                    strikes += 1
                    await bus.emit_warning(
                        message=f"无效工具调用已拒绝（第 {strikes}/2 次）: "
                        f"{[c.name or '<空>' for c in invalid]}"
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": response.text or "",
                            "tool_calls": [_as_openai_call(c) for c in calls],
                        }
                    )
                    for call in invalid:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id or "unknown",
                                "content": "错误：工具调用无效（未知工具或参数不是合法 JSON）。请使用可用工具重试。",
                            }
                        )
                    if strikes >= 2:
                        await bus.emit_error(message="模型连续产生无效工具调用", recoverable=False)
                        outcome.completed = False
                        return outcome
                    continue

            # ④ 工具执行与轮数门控
            if calls:
                if tool_rounds >= deps.max_rounds:
                    if not finish_requested:
                        finish_requested = True
                        await bus.emit_warning(message="已达工具调用轮数上限，要求模型直接收尾")
                        messages.append(
                            {
                                "role": "user",
                                "content": "（系统）已达到工具调用轮数上限，请直接给出最终回答，不要继续调用工具。",
                            }
                        )
                        continue
                    await bus.emit_error(message="超出工具轮数上限", recoverable=False)
                    outcome.completed = False
                    return outcome
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.text or "",
                        "tool_calls": [_as_openai_call(c) for c in calls],
                    }
                )
                tool_rounds += 1  # 一轮 = 一次含工具调用的 LLM 轮（批内多调用算一轮）
                for index, call in enumerate(calls):
                    call_id = call.id or f"call-{tool_rounds}-{index}"
                    args = _json_or_none(call.arguments) or {}
                    await bus.emit_tool_call(tool_name=call.name, args=args, call_id=call_id)
                    tool_ctx = ToolContext(
                        turn_id=bus.turn_id,
                        session_id=bus.session_id or "",
                        language=ctx.language,
                        config=ctx.config,
                        # cost_tracker 注入：工具内次级 LLM 调用（brainstorm/reason 等）
                        # 的用量并入本回合成本（§6.9）
                        metadata={**ctx.metadata, "cost_tracker": ctx.cost},
                        args=args,
                    )
                    result = await deps.tools.run(call.name, tool_ctx)
                    await bus.emit_tool_result(
                        call_id=call_id,
                        ok=result.ok,
                        summary=result.output,
                        detail=result.detail,
                        usage=result.usage,
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": result.output}
                    )
                    outcome.tool_calls.append(
                        {
                            "tool_name": call.name,
                            "call_id": call_id,
                            "ok": result.ok,
                            "summary": result.output[:200],
                        }
                    )
                    if result.detail and "sources" in result.detail:
                        outcome.citations.extend(result.detail["sources"])
                continue

            # ⑤ 无工具调用 → responding → done
            answer_parts.append(response.text)
            outcome.final_text = "".join(answer_parts)
            outcome.thinking = "".join(thinking_parts)
            await bus.emit_content_done(full_text=outcome.final_text)
            return outcome
    except asyncio.CancelledError:
        await bus.emit_stopped()
        outcome.completed = False
        raise


def _record_cost(
    ctx: "UnifiedContext", deps: LoopDeps, response: LLMResponse, messages: list[dict[str, Any]]
) -> None:
    """usage 帧记账；无帧回退字符估算（§6.9）。"""
    if response.usage:
        ctx.cost.add_usage(
            provider=deps.provider,
            model=deps.model,
            input_tokens=response.usage.get("prompt_tokens", 0),
            output_tokens=response.usage.get("completion_tokens", 0),
        )
    elif response.text:
        ctx.cost.add_estimated(
            provider=deps.provider,
            model=deps.model,
            input_chars=round(total_tokens(messages) * 3.5),
            output_chars=len(response.text),
        )
