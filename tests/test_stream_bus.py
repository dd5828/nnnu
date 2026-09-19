"""流总线：发布有序、回放与实时无重复、过滤、关闭哨兵、异常隔离。"""

import asyncio

import pytest

from nnnu.core.events import StreamEventType
from nnnu.core.stream_bus import StreamBus, get_bus, register_bus, unregister_bus


async def test_publish_subscribe_order():
    bus = StreamBus("turn-1", session_id="sess-1")
    gen = bus.subscribe()
    e1 = await bus.emit_content_delta(text="a")
    e2 = await bus.emit_content_delta(text="b")
    got = []
    async for event in gen:
        got.append(event)
        if len(got) == 2:
            break
    assert [e.event_id for e in got] == [e1.event_id, e2.event_id]


async def test_replay_and_live_no_duplicate():
    bus = StreamBus("turn-1")
    await bus.emit_content_delta(text="1")
    await bus.emit_content_delta(text="2")
    # 注册时回放 after=1（跳过第 1 条），随后实时事件恰好到达
    gen = bus.subscribe(after=1)
    await bus.emit_content_delta(text="3")
    got = []
    async for event in gen:
        got.append(event)
        if len(got) == 2:
            break
    texts = [e.payload["text"] for e in got]
    assert texts == ["2", "3"], f"回放与实时双投或漏投: {texts}"


async def test_type_filter_replay_and_live():
    bus = StreamBus("turn-1")
    await bus.emit_content_delta(text="x")
    await bus.emit_warning(message="w")
    gen = bus.subscribe(types={StreamEventType.WARNING})
    await bus.emit_content_delta(text="y")
    await bus.emit_warning(message="w2")
    got = []
    async for event in gen:
        got.append(event)
        if len(got) == 2:
            break
    assert [e.type for e in got] == [StreamEventType.WARNING, StreamEventType.WARNING]


async def test_close_sends_sentinel():
    bus = StreamBus("turn-1")
    await bus.emit_content_delta(text="a")
    gen = bus.subscribe()
    await bus.emit_content_delta(text="b")
    bus.mark_closed()
    got = []
    async for event in gen:
        got.append(event)
    assert len(got) == 2
    # 关闭后 publish 丢弃
    await bus.emit_content_delta(text="c")
    assert len(bus.history) == 2


async def test_subscribe_after_close_replays_only():
    bus = StreamBus("turn-1")
    await bus.emit_content_delta(text="a")
    bus.mark_closed()
    got = []
    async for event in bus.subscribe():
        got.append(event)
    assert len(got) == 1


async def test_consumer_exception_isolated():
    bus = StreamBus("turn-1")

    class BadConsumer:
        async def on_event(self, event) -> None:
            raise RuntimeError("boom")

    class GoodConsumer:
        def __init__(self) -> None:
            self.received: list = []

        async def on_event(self, event) -> None:
            self.received.append(event)

    bus.add_consumer(BadConsumer())
    good = GoodConsumer()
    bus.add_consumer(good)
    await bus.emit_content_delta(text="hi")
    await asyncio.sleep(0)  # 让转发任务跑一拍
    # 坏订阅者异常被隔离，好订阅者正常收到事件
    assert len(good.received) == 1
    assert good.received[0].payload["text"] == "hi"


async def test_slow_consumer_does_not_block_publish():
    bus = StreamBus("turn-1")
    bus.subscribe()  # 注册一个从不消费的订阅者（无界队列）
    for i in range(50):
        await bus.emit_content_delta(text=str(i))
    assert len(bus.history) == 50


async def test_terminal_emitted_once():
    bus = StreamBus("turn-1")
    await bus.emit_done(response="ok")
    assert bus.terminal_emitted
    await bus.emit_error(message="late")  # 终局后不再接受第二个终局事件
    assert len(bus.history) == 1
    # 非终局事件仍可发布（如收尾告警）
    await bus.emit_warning(message="after")
    assert len(bus.history) == 2


async def test_bus_registry_roundtrip():
    bus = StreamBus("turn-9")
    register_bus("turn-9", bus)
    assert get_bus("turn-9") is bus
    unregister_bus("turn-9")
    assert get_bus("turn-9") is None


async def test_emit_helpers_produce_correct_types():
    bus = StreamBus("turn-1", session_id="sess-1")
    await bus.emit_turn_start(capability="chat", model="m")
    await bus.emit_status(stage="planning")
    await bus.emit_thinking_delta(text="t")
    await bus.emit_tool_call(tool_name="add", args={"a": 1}, call_id="c1")
    await bus.emit_tool_result(call_id="c1", ok=True, summary="=2")
    await bus.emit_citation(sources=[{"doc_id": "d", "kb": "k"}])
    await bus.emit_ask_user(question="q", options=[], ask_id="a1")
    await bus.emit_ask_user_reply(ask_id="a1", answer="x")
    await bus.emit_cost_summary(tokens=1, cost=0.0, per_model={})
    types = [e.type for e in bus.history]
    assert types == [
        StreamEventType.TURN_START,
        StreamEventType.STATUS,
        StreamEventType.THINKING_DELTA,
        StreamEventType.TOOL_CALL,
        StreamEventType.TOOL_RESULT,
        StreamEventType.CITATION,
        StreamEventType.ASK_USER,
        StreamEventType.ASK_USER_REPLY,
        StreamEventType.COST_SUMMARY,
    ]
    # session_id 由 bus 统一携带
    assert all(e.session_id == "sess-1" for e in bus.history)
    assert bus.history[0].payload_model().model == "m"


@pytest.fixture(autouse=True)
def _clean_bus_registry():
    """测试间清空模块级注册表。"""
    from nnnu.core import stream_bus

    for turn_id in list(stream_bus._buses):
        stream_bus.unregister_bus(turn_id)
    yield
    for turn_id in list(stream_bus._buses):
        stream_bus.unregister_bus(turn_id)
