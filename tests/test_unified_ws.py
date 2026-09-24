"""WS 全协议：事件序、双连接隔离、心跳、stop、断线补发（验收③）、合成 done。"""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.core.events import StreamEventType
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMChunk, LLMRequest
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


@pytest.fixture
def ws_client(tmp_home):
    """TestClient 跑 lifespan（数据库/运行时装配在 portal 线程事件循环）。"""
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _receive_until(ws, predicate, limit=100) -> list[dict]:
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"未等到目标事件，已收 {len(events)} 条")


async def test_event_ordering_per_connection(ws_client):
    install_scripted(
        lambda: ScriptedLLM(
            [
                ScriptedStep(
                    chunks=["你好，世界"], usage={"prompt_tokens": 3, "completion_tokens": 2}
                )
            ]
        )
    )
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "chat", "message": "你好"})
        events = _receive_until(ws, lambda e: e["type"] == "done")
    types = [e["type"] for e in events]
    assert types[0] == "turn_start"
    assert "content_delta" in types
    assert "cost_summary" in types
    assert types[-1] == "done"
    # seq 单调
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)


async def test_two_connections_isolated(ws_client):
    calls = []

    def factory():
        calls.append(1)
        return ScriptedLLM([ScriptedStep(chunks=[f"回答{len(calls)}"])])

    install_scripted(factory)
    with (
        ws_client.websocket_connect("/api/v1/ws") as ws1,
        ws_client.websocket_connect("/api/v1/ws") as ws2,
    ):
        ws1.send_json({"type": "chat", "message": "问题A"})
        events1 = _receive_until(ws1, lambda e: e["type"] == "done")
        ws2.send_json({"type": "chat", "message": "问题B"})
        events2 = _receive_until(ws2, lambda e: e["type"] == "done")
    assert events1[-1]["payload"]["response"] == "回答1"
    assert events2[-1]["payload"]["response"] == "回答2"
    # 互不串流：两回合 turn_id 不同且各自事件都属自己
    turn1 = events1[0]["turn_id"]
    turn2 = events2[0]["turn_id"]
    assert turn1 != turn2
    assert all(e["turn_id"] == turn1 for e in events1)
    assert all(e["turn_id"] == turn2 for e in events2)


async def test_ping_heartbeat(ws_client):
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "ping"})
        event = ws.receive_json()
        assert event["type"] == "heartbeat"


async def test_busy_error_recoverable(ws_client):
    release = threading.Event()

    class SlowClient:
        async def complete(self, request: LLMRequest):
            raise NotImplementedError

        async def stream(self, request: LLMRequest):
            yield LLMChunk(text="部分")
            await asyncio.to_thread(release.wait)
            yield LLMChunk(finish_reason="stop")

    install_scripted(lambda: SlowClient())
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "chat", "session_id": "sess-x", "message": "第一个"})
        _receive_until(ws, lambda e: e["type"] == "content_delta")
        # 同 session 并发第二个 → busy error（recoverable=true）
        ws.send_json({"type": "chat", "session_id": "sess-x", "message": "第二个"})
        busy = _receive_until(ws, lambda e: e["type"] == "error")
        assert busy[-1]["payload"]["recoverable"] is True
        release.set()


async def test_stop_turn(ws_client):
    release = threading.Event()

    class SlowClient:
        async def complete(self, request: LLMRequest):
            raise NotImplementedError

        async def stream(self, request: LLMRequest):
            yield LLMChunk(text="部分")
            await asyncio.to_thread(release.wait)
            yield LLMChunk(finish_reason="stop")

    install_scripted(lambda: SlowClient())
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "chat", "message": "停我"})
        events = _receive_until(ws, lambda e: e["type"] == "content_delta")
        turn_id = events[0]["turn_id"]
        ws.send_json({"type": "stop", "turn_id": turn_id})
        stopped = _receive_until(ws, lambda e: e["type"] == "stopped")
        assert stopped[-1]["turn_id"] == turn_id
        release.set()


async def test_reconnect_replays_buffered_events(ws_client):
    """验收③：断线后回合后台继续，重连 resume 从断点补发，seq 连续。"""
    release = threading.Event()

    class SlowClient:
        async def complete(self, request: LLMRequest):
            raise NotImplementedError

        async def stream(self, request: LLMRequest):
            yield LLMChunk(text="第一块")
            await asyncio.to_thread(release.wait)
            yield LLMChunk(text="第二块")
            yield LLMChunk(finish_reason="stop")

    install_scripted(lambda: SlowClient())
    with ws_client.websocket_connect("/api/v1/ws") as ws1:
        ws1.send_json({"type": "chat", "message": "长回答"})
        events1 = _receive_until(ws1, lambda e: e["type"] == "content_delta")
        session_id = events1[0]["session_id"]
        last_seq = events1[-1]["seq"]
    # ws1 断开（回合后台继续，阻塞在 release）
    with ws_client.websocket_connect("/api/v1/ws") as ws2:
        ws2.send_json({"type": "resume", "session_id": session_id, "after_seq": last_seq})
        # 尚无新事件（回合仍阻塞）——不空转等待；先放行回合
        release.set()
        events2 = _receive_until(ws2, lambda e: e["type"] == "done", limit=100)
    # 补发事件从断点连续（无重复无缺口）
    seqs2 = [e["seq"] for e in events2 if e["seq"]]
    assert seqs2[0] == last_seq + 1
    assert seqs2 == list(range(last_seq + 1, seqs2[-1] + 1))
    assert events2[-1]["type"] == "done"
    assert events2[-1]["payload"]["response"] == "第一块第二块"


async def test_resume_finished_synthesized_done(ws_client):
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["已完成回答"])]))
    session_id = None
    with ws_client.websocket_connect("/api/v1/ws") as ws1:
        ws1.send_json({"type": "chat", "message": "问题"})
        events1 = _receive_until(ws1, lambda e: e["type"] == "done")
        session_id = events1[0]["session_id"]
    # 等回合完全收尾（迁入 _finished）后，模拟 LRU 淘汰/服务重启：
    # 清空内存缓冲 → resume 走会话快照合成路径
    import time

    runtime = ws_client.app.state.runtime
    for _ in range(100):
        if not runtime._executions:
            break
        time.sleep(0.05)
    runtime._finished.clear()
    with ws_client.websocket_connect("/api/v1/ws") as ws2:
        ws2.send_json({"type": "resume", "session_id": session_id})
        events2 = _receive_until(ws2, lambda e: e["type"] == "done")
    done = events2[-1]
    assert done["payload"]["synthesized"] is True
    assert done["payload"]["response"] == "已完成回答"


async def test_unknown_type_error(ws_client):
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "bogus"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["payload"]["recoverable"] is False


async def test_turn_start_reports_effective_model(ws_client):
    """§6.10：turn_start 的 model 是生效值——当回合报显式选择，之后的回合报会话里存的。"""
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["回答"])]))
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json(
            {
                "type": "chat",
                "session_id": "sess-ws-model",
                "message": "一",
                "model": "kimi:kimi-k2",
            }
        )
        events = _receive_until(ws, lambda e: e["type"] == "done")
    assert events[0]["payload"]["model"] == "kimi:kimi-k2"

    # done 先发、会话互斥在收尾时才解除：不等它，下一发会被挡成 busy error 帧
    import time

    runtime = ws_client.app.state.runtime
    for _ in range(100):
        if runtime.active_turn_for("sess-ws-model") is None:
            break
        time.sleep(0.05)

    # 新连接、不带 model：沿用会话里的选择（粘性）
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "chat", "session_id": "sess-ws-model", "message": "二"})
        events = _receive_until(ws, lambda e: e["type"] == "done")
    assert events[0]["payload"]["model"] == "kimi:kimi-k2"


async def test_invalid_model_ref_returns_error_frame(ws_client):
    """坏 model 引用 fail-fast 成 error 帧，连接不断（后续消息照常处理）。"""
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "chat", "message": "问题", "model": "nope:x"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert "未知模型引用" in event["payload"]["message"]

        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "heartbeat"
