"""Agent 循环：验收①事件序列、轮数门控、截断续写、空调用拒绝、DSML、ask_user。"""

import asyncio

import pytest

from nnnu.core.agent_loop import LoopDeps, ToolSet, run_agent_loop
from nnnu.core.context import SessionRef, UnifiedContext
from nnnu.core.events import StreamEventType
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.sessions.models import Message


class AddTool(BaseTool):
    definition = ToolDefinition(
        name="add_numbers",
        description="整数加法",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        mount=ToolMount.ALWAYS,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output=str(ctx.args["a"] + ctx.args["b"]))


class MultiplyTool(BaseTool):
    definition = ToolDefinition(
        name="multiply",
        description="整数乘法",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
        mount=ToolMount.ALWAYS,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output=str(ctx.args["x"] * ctx.args["y"]))


TOOLS = ToolSet({"add_numbers": AddTool(), "multiply": MultiplyTool()})


def _ctx(**metadata):
    return UnifiedContext(
        session=SessionRef(id="sess-1"),
        capability="chat",
        message=Message.new(session_id="sess-1", role="user", content="问题"),
        metadata=metadata,
    )


def _deps(llm, **overrides):
    base = dict(client=llm, tools=TOOLS, model="deepseek-chat", provider="deepseek")
    base.update(overrides)
    return LoopDeps(**base)


async def _run_and_collect(llm, deps_overrides=None, ctx_metadata=None, history=None):
    """跑循环并同步收集事件（订阅先于运行，避免竞态）。"""
    bus = StreamBus("turn-1", session_id="sess-1")
    ctx = _ctx(**(ctx_metadata or {}))
    deps = _deps(llm, **(deps_overrides or {}))
    history = history or [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "问题"},
    ]
    events: list = []
    gen = bus.subscribe()

    async def _collect():
        async for event in gen:
            events.append(event)

    collector = asyncio.create_task(_collect())
    outcome = await run_agent_loop(ctx, bus, deps, history)
    bus.mark_closed()
    await collector
    return outcome, events


def _types(events):
    return [e.type for e in events]


async def test_scripted_thinking_two_tools_answer_sequence():
    """验收①核心：思考 → 调 2 个工具 → 回答，事件序列断言。"""
    llm = ScriptedLLM(
        [
            ScriptedStep(
                thinking=["先想"],
                tool_calls=[
                    LLMToolCall(id="c1", name="add_numbers", arguments='{"a": 1, "b": 2}'),
                    LLMToolCall(id="c2", name="multiply", arguments='{"x": 3, "y": 4}'),
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 50, "completion_tokens": 10},
            ),
            ScriptedStep(
                chunks=["结果是 3 和 12"], usage={"prompt_tokens": 40, "completion_tokens": 5}
            ),
        ]
    )
    outcome, events = await _run_and_collect(llm)
    assert _types(events) == [
        StreamEventType.THINKING_DELTA,
        StreamEventType.THINKING_DONE,
        StreamEventType.TOOL_CALL,
        StreamEventType.TOOL_RESULT,
        StreamEventType.TOOL_CALL,
        StreamEventType.TOOL_RESULT,
        StreamEventType.CONTENT_DELTA,
        StreamEventType.CONTENT_DONE,
    ]
    assert events[2].payload["tool_name"] == "add_numbers"
    assert events[3].payload["ok"] is True
    assert events[3].payload["summary"] == "3"
    assert events[5].payload["summary"] == "12"
    assert outcome.final_text == "结果是 3 和 12"
    assert outcome.completed
    # 思考在正文之前（事件序已保证）；工具轨迹与成本记账
    assert [t["tool_name"] for t in outcome.tool_calls] == ["add_numbers", "multiply"]
    summary = _ctx().cost.summary()  # 空 tracker 不参与断言
    assert summary["tokens"] == 0
    # 真实 ctx 的成本在 _run_and_collect 内部——改为断言 LLM 调用数
    assert len(llm.calls) == 2


async def test_cost_recorded_into_ctx():
    llm = ScriptedLLM(
        [ScriptedStep(chunks=["答"], usage={"prompt_tokens": 7, "completion_tokens": 3})]
    )
    bus = StreamBus("turn-1", session_id="sess-1")
    ctx = _ctx()
    deps = _deps(llm)
    events = []
    gen = bus.subscribe()

    async def _collect():
        async for event in gen:
            events.append(event)

    collector = asyncio.create_task(_collect())
    await run_agent_loop(ctx, bus, deps, [{"role": "user", "content": "问"}])
    bus.mark_closed()
    await collector
    summary = ctx.cost.summary()
    assert summary["tokens"] == 10
    assert summary["per_model"]["deepseek-chat"]["calls"] == 1


async def test_max_rounds_force_finish_then_error():
    tool = LLMToolCall(id="c", name="add_numbers", arguments='{"a": 1, "b": 1}')
    llm = ScriptedLLM(
        [
            ScriptedStep(tool_calls=[tool], finish_reason="tool_calls"),
            ScriptedStep(tool_calls=[tool], finish_reason="tool_calls"),
            ScriptedStep(tool_calls=[tool], finish_reason="tool_calls"),
        ]
    )
    outcome, events = await _run_and_collect(llm, deps_overrides={"max_rounds": 1})
    types = _types(events)
    # 第一轮执行 1 次工具；第二轮超限 → warning + 收尾指令；第三轮仍调 → error
    assert types.count(StreamEventType.TOOL_CALL) == 1
    assert StreamEventType.WARNING in types
    assert types[-1] == StreamEventType.ERROR
    assert events[-1].payload["recoverable"] is False
    assert not outcome.completed


async def test_truncated_continuation_single_stream():
    llm = ScriptedLLM(
        [
            ScriptedStep(chunks=["第一段"], finish_reason="length"),
            ScriptedStep(chunks=["第二段"], finish_reason="stop"),
        ]
    )
    outcome, events = await _run_and_collect(llm)
    texts = [e.payload["text"] for e in events if e.type == StreamEventType.CONTENT_DELTA]
    assert texts == ["第一段", "第二段"]
    done = [e for e in events if e.type == StreamEventType.CONTENT_DONE]
    assert done[0].payload["full_text"] == "第一段第二段"
    assert outcome.final_text == "第一段第二段"


async def test_truncated_continuation_exhausted_finishes_partial():
    llm = ScriptedLLM(
        [
            ScriptedStep(chunks=["段1"], finish_reason="length"),
            ScriptedStep(chunks=["段2"], finish_reason="length"),
            ScriptedStep(chunks=["段3"], finish_reason="length"),
        ]
    )
    outcome, events = await _run_and_collect(llm)
    # 续写上限 2 次用尽 → 按已收内容收尾（warning + content_done）
    assert StreamEventType.WARNING in _types(events)
    assert outcome.final_text == "段1段2段3"
    assert outcome.completed


async def test_empty_tool_call_two_strikes():
    bad1 = LLMToolCall(id="c1", name="", arguments="{}")
    bad2 = LLMToolCall(id="c2", name="no_such_tool", arguments="{}")
    llm = ScriptedLLM(
        [
            ScriptedStep(tool_calls=[bad1], finish_reason="tool_calls"),
            ScriptedStep(tool_calls=[bad2], finish_reason="tool_calls"),
        ]
    )
    outcome, events = await _run_and_collect(llm)
    types = _types(events)
    assert types.count(StreamEventType.TOOL_CALL) == 0  # 无效调用不执行
    assert types.count(StreamEventType.WARNING) == 2
    assert types[-1] == StreamEventType.ERROR
    assert not outcome.completed


async def test_invalid_arguments_rejected_once_then_recovers():
    bad = LLMToolCall(id="c1", name="add_numbers", arguments="不是JSON")
    llm = ScriptedLLM(
        [
            ScriptedStep(tool_calls=[bad], finish_reason="tool_calls"),
            ScriptedStep(chunks=["改用正确格式回答"]),
        ]
    )
    outcome, events = await _run_and_collect(llm)
    types = _types(events)
    # 第 1 次 strike 拒绝 → 模型改正 → 正常收尾
    assert types.count(StreamEventType.WARNING) == 1
    assert types.count(StreamEventType.TOOL_CALL) == 0
    assert types[-1] == StreamEventType.CONTENT_DONE
    assert outcome.final_text == "改用正确格式回答"
    assert outcome.completed


async def test_dsml_tool_calls_executed_and_text_kept():
    dsml_text = (
        "说明<||DSML||function_calls>"
        '<||DSML||invoke name="add_numbers">{"a": 2, "b": 3}</||DSML||invoke>'
        "</||DSML||function_calls>结尾"
    )
    llm = ScriptedLLM(
        [
            ScriptedStep(chunks=[dsml_text], finish_reason="stop"),
            ScriptedStep(chunks=["答案是 5"]),
        ]
    )
    outcome, events = await _run_and_collect(llm)
    types = _types(events)
    assert types == [
        StreamEventType.CONTENT_DELTA,  # "说明"（块剥除后）
        StreamEventType.CONTENT_DELTA,  # "结尾"
        StreamEventType.TOOL_CALL,
        StreamEventType.TOOL_RESULT,
        StreamEventType.CONTENT_DELTA,
        StreamEventType.CONTENT_DONE,
    ]
    # 正文不丢：DSML 标记剥除，散文保留
    text_parts = [e.payload["text"] for e in events if e.type == StreamEventType.CONTENT_DELTA]
    assert text_parts[0] == "说明"
    assert text_parts[1] == "结尾"
    assert events[2].payload["tool_name"] == "add_numbers"
    assert events[3].payload["summary"] == "5"


async def test_ask_user_pause_resume():
    asked: list[tuple] = []

    class PauseTool(BaseTool):
        definition = ToolDefinition(
            name="ask_user",
            description="向用户提问",
            parameters={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            mount=ToolMount.ALWAYS,
        )

        async def run(self, ctx: ToolContext) -> ToolResult:
            answer = await ctx.metadata["ask_user_fn"](
                ctx.args["question"], [], f"ask-{ctx.turn_id}"
            )
            return ToolResult(ok=True, output=answer)

    async def ask_fn(question, options, ask_id):
        asked.append((question, options, ask_id))
        return "选A"

    tools = ToolSet({"add_numbers": AddTool(), "multiply": MultiplyTool(), "ask_user": PauseTool()})
    llm = ScriptedLLM(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(id="c1", name="ask_user", arguments='{"question": "选哪个?"}')
                ],
                finish_reason="tool_calls",
            ),
            ScriptedStep(chunks=["收到，答案是A"]),
        ]
    )
    bus = StreamBus("turn-1", session_id="sess-1")
    ctx = _ctx(ask_user_fn=ask_fn)
    deps = _deps(llm)
    deps.tools = tools
    events = []
    gen = bus.subscribe()

    async def _collect():
        async for event in gen:
            events.append(event)

    collector = asyncio.create_task(_collect())
    outcome = await run_agent_loop(ctx, bus, deps, [{"role": "user", "content": "问"}])
    bus.mark_closed()
    await collector
    assert asked and asked[0][0] == "选哪个?"
    assert outcome.final_text == "收到，答案是A"
    # 工具结果就是用户答复
    tool_results = [e for e in events if e.type == StreamEventType.TOOL_RESULT]
    assert tool_results[0].payload["summary"] == "选A"


class _BlockingClient:
    """永远阻塞的流式客户端：验证取消路径。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(self, request):
        raise NotImplementedError

    async def stream(self, request):
        self.started.set()
        await asyncio.Event().wait()  # 永久阻塞
        if False:  # pragma: no cover
            yield None


async def test_cancelled_emits_stopped_and_reraises():
    client = _BlockingClient()
    bus = StreamBus("turn-1")
    ctx = _ctx()
    deps = _deps(None)  # type: ignore[arg-type]
    deps.client = client  # type: ignore[assignment]
    events = []
    gen = bus.subscribe()

    async def _collect():
        async for event in gen:
            events.append(event)

    collector = asyncio.create_task(_collect())
    task = asyncio.create_task(run_agent_loop(ctx, bus, deps, [{"role": "user", "content": "问"}]))
    await client.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bus.mark_closed()
    await collector
    assert StreamEventType.STOPPED in _types(events)
