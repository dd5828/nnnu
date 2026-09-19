"""编排器 + TurnRuntime 全链路：验收①事件序列、互斥、stop、regenerate、ask_user 超时。"""

import asyncio

import pytest

from nnnu.core.events import StreamEventType
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.runtime import bootstrap
from nnnu.runtime.orchestrator import ChatOrchestrator, TurnBusyError
from nnnu.runtime.registry.capability_registry import get_capability_registry
from nnnu.runtime.registry.tool_registry import get_tool_registry
from nnnu.runtime.turn_runtime import ASK_TIMEOUT_S, TurnRequest, TurnRuntimeManager
from nnnu.services.cost.service import CostService
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.sessions.db import Database
from nnnu.services.sessions.schema import db_path, migrate
from nnnu.services.sessions.service import SessionManager


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


@pytest.fixture
async def runtime(tmp_home):
    data_root = tmp_home / "data"
    migrate(data_root)
    db = Database(db_path(data_root))
    await db.connect()
    try:
        sessions = SessionManager(db)
        costs = CostService(db)
        bootstrap.register_builtins()
        get_tool_registry().register(AddTool())
        get_tool_registry().register(MultiplyTool())
        orchestrator = ChatOrchestrator(
            capabilities=get_capability_registry(), tools=get_tool_registry()
        )
        manager = TurnRuntimeManager(sessions=sessions, costs=costs, orchestrator=orchestrator)
        yield manager, sessions, costs, db
    finally:
        await db.close()


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _scripted(steps):
    install_scripted(lambda: ScriptedLLM(steps))


async def _wait_turn(manager, turn_id, timeout=5.0):
    execution = manager._executions[turn_id]
    await asyncio.wait_for(execution.task, timeout=timeout)


async def _collect_turn(manager, turn_id):
    events = []
    async for event in manager.subscribe_turn(turn_id):
        events.append(event)
    return events


async def test_scripted_full_turn_event_sequence(runtime):
    """验收①：mock LLM 驱动「思考→调 2 个工具→回答」全链路，经编排器/传输层。"""
    manager, sessions, costs, _ = runtime
    _scripted(
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
    started = await manager.start_turn(TurnRequest(message="请计算"))
    turn_id = started["turn_id"]
    events_task = asyncio.create_task(_collect_turn(manager, turn_id))
    await _wait_turn(manager, turn_id)
    events = await events_task
    types = [e["type"] for e in events]
    assert types == [
        StreamEventType.TURN_START.value,
        StreamEventType.STATUS.value,
        StreamEventType.THINKING_DELTA.value,
        StreamEventType.THINKING_DONE.value,
        StreamEventType.TOOL_CALL.value,
        StreamEventType.TOOL_RESULT.value,
        StreamEventType.TOOL_CALL.value,
        StreamEventType.TOOL_RESULT.value,
        StreamEventType.CONTENT_DELTA.value,
        StreamEventType.CONTENT_DONE.value,
        StreamEventType.COST_SUMMARY.value,
        StreamEventType.DONE.value,
    ]
    # seq 单调且从 1 开始（§7.20 事件有序）
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    # 统一信封：cost_summary 在 done 之前（§6.1）
    assert events[-2]["type"] == StreamEventType.COST_SUMMARY.value
    assert events[-1]["payload"]["response"] == "结果是 3 和 12"
    # assistant 消息落库（§6.8 会话快照）
    session_id = started["session_id"] or events[0]["session_id"]
    messages = await sessions.list_messages(session_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "结果是 3 和 12"
    assert messages[1].tool_calls  # 工具轨迹
    # 成本摘要落库（验收④）
    summary = await costs.query_summary(days=7)
    assert summary["total_tokens"] == 105
    assert summary["per_model"][0]["model"] == "deepseek-chat"


async def test_busy_session_error(runtime):
    manager, _, _, _ = runtime
    _scripted([ScriptedStep(chunks=["答"])])
    first = await manager.start_turn(TurnRequest(session_id="sess-busy", message="第一个"))
    await _wait_turn(manager, first["turn_id"])
    # 已完成回合不占互斥 → 新回合可开
    second = await manager.start_turn(TurnRequest(session_id="sess-busy", message="第二个"))
    assert second["turn_id"] != first["turn_id"]


async def test_busy_session_concurrent_rejected(runtime):
    manager, _, _, _ = runtime

    class BlockingClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, request):
            raise NotImplementedError

        async def stream(self, request):
            self.started.set()
            await asyncio.Event().wait()
            if False:
                yield None

    install_scripted(BlockingClient)
    first = await manager.start_turn(TurnRequest(session_id="sess-lock", message="第一个"))
    # 等回合真正开始跑（turn_start 已入缓冲）
    while not manager._executions[first["turn_id"]].events:
        await asyncio.sleep(0.01)
    with pytest.raises(TurnBusyError):
        await manager.start_turn(TurnRequest(session_id="sess-lock", message="第二个"))
    await manager.stop_turn(first["turn_id"])


async def test_stop_emits_stopped(runtime):
    manager, sessions, _, _ = runtime

    class BlockingClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, request):
            raise NotImplementedError

        async def stream(self, request):
            self.started.set()
            await asyncio.Event().wait()
            if False:
                yield None

    install_scripted(BlockingClient)
    started = await manager.start_turn(TurnRequest(session_id="sess-stop", message="停我"))
    turn_id = started["turn_id"]
    while not manager._executions[turn_id].events:
        await asyncio.sleep(0.01)
    stopped = await manager.stop_turn(turn_id)
    assert stopped is True
    execution = manager._finished.get(turn_id)
    types = [e["type"] for e in execution.events]
    assert StreamEventType.STOPPED.value in types
    # 用户消息已落库；无 assistant 行（无产出）
    messages = await sessions.list_messages("sess-stop")
    assert [m.role for m in messages] == ["user"]


async def test_regenerate_reruns_last_user(runtime):
    manager, sessions, _, _ = runtime
    calls = []

    def factory():
        calls.append(1)
        return ScriptedLLM([ScriptedStep(chunks=[f"回答{len(calls)}"])])

    install_scripted(factory)
    first = await manager.start_turn(TurnRequest(session_id="sess-regen", message="问题"))
    await _wait_turn(manager, first["turn_id"])
    second = await manager.regenerate("sess-regen")
    await _wait_turn(manager, second["turn_id"])
    messages = await sessions.list_messages("sess-regen")
    # 旧 assistant 被丢弃：只剩 user + 新 assistant
    assert [m.content for m in messages] == ["问题", "回答2"]
    assert len(calls) == 2


async def test_ask_user_timeout_empty_reply(runtime, monkeypatch):
    manager, sessions, _, _ = runtime
    monkeypatch.setattr("nnnu.runtime.turn_runtime.ASK_TIMEOUT_S", 0.2)
    _scripted(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(id="c1", name="ask_user", arguments='{"question": "选哪个?"}')
                ],
                finish_reason="tool_calls",
            ),
            ScriptedStep(chunks=["收到空答复"]),
        ]
    )
    started = await manager.start_turn(TurnRequest(session_id="sess-ask", message="问一下"))
    turn_id = started["turn_id"]
    # 关键：等待期间不订阅（订阅者会取消超时计时）——回合结束后重放缓冲
    await _wait_turn(manager, turn_id, timeout=10)
    events = manager._finished[turn_id].events
    types = [e["type"] for e in events]
    assert StreamEventType.ASK_USER.value in types
    assert StreamEventType.ASK_USER_REPLY.value in types
    reply = next(e for e in events if e["type"] == StreamEventType.ASK_USER_REPLY.value)
    assert reply["payload"]["answer"] == ""  # 5 分钟超时（测试缩短）→ 空答复
    messages = await sessions.list_messages("sess-ask")
    assert messages[-1].content == "收到空答复"


async def test_ask_user_reply_via_submit(runtime, monkeypatch):
    manager, _, _, _ = runtime
    monkeypatch.setattr("nnnu.runtime.turn_runtime.ASK_TIMEOUT_S", 30.0)
    _scripted(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(id="c1", name="ask_user", arguments='{"question": "选哪个?"}')
                ],
                finish_reason="tool_calls",
            ),
            ScriptedStep(chunks=["收到选B"]),
        ]
    )
    started = await manager.start_turn(TurnRequest(session_id="sess-ask2", message="问一下"))
    turn_id = started["turn_id"]
    # 等 ask_user 事件出现
    while not any(
        e["type"] == StreamEventType.ASK_USER.value for e in manager._executions[turn_id].events
    ):
        await asyncio.sleep(0.01)
    ask_event = next(
        e
        for e in manager._executions[turn_id].events
        if e["type"] == StreamEventType.ASK_USER.value
    )
    # 订阅者到达会取消超时计时；此处直接答复
    assert await manager.submit_user_reply(turn_id, ask_event["payload"]["ask_id"], "选B")
    await _wait_turn(manager, turn_id)
    events = manager._finished[turn_id].events
    reply = next(e for e in events if e["type"] == StreamEventType.ASK_USER_REPLY.value)
    assert reply["payload"]["answer"] == "选B"
    assert events[-1]["payload"]["response"] == "收到选B"
