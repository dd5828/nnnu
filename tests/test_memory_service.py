"""MemoryService（§7.10 刀三接缝）：L1 采集、回合注册表、单飞、自动触发、生命周期。"""

import asyncio
import json
import time

import pytest

import nnnu.services.memory.service as memory_service_module
from nnnu.core.events import StreamEventType
from nnnu.core.stream_bus import StreamBus
from nnnu.services.memory import paths
from nnnu.services.memory.models import ConsolidationRun, MemoryConfig
from nnnu.services.memory.service import (
    MemoryService,
    get_memory_service,
    set_memory_service,
)

pytestmark = pytest.mark.usefixtures("tmp_home", "repo_prompts")


async def _record(service: MemoryService, turn_id: str = "turn-1", *, text: str = "你好") -> None:
    """走一遍真实接缝：begin_turn → bus 事件 → record_turn。"""
    await service.begin_turn(turn_id, "sess-1", "chat", text)
    bus = StreamBus(turn_id, session_id="sess-1")
    await bus.emit_done(response="从直觉讲起", status="completed")
    await bus.emit_cost_summary(
        tokens=10,
        cost=0.001,
        per_model={
            "deepseek-chat": {
                "provider": "deepseek",
                "input_tokens": 6,
                "output_tokens": 4,
                "cost": 0.001,
            }
        },
    )
    await service.record_turn(turn_id, "sess-1", bus.history)


def _trace_rows(data_root, surface: str = "chat") -> list[dict]:
    month = time.strftime("%Y-%m", time.localtime())
    path = paths.trace_path(data_root, surface, month)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def test_begin_and_record_turn_write_l1_rows(tmp_path):
    service = MemoryService(tmp_path)
    await _record(service, "turn-1")

    rows = _trace_rows(tmp_path)
    assert [row["event"] for row in rows] == ["user_message", "assistant_done", "cost"]
    assert [row["data"]["seq"] for row in rows] == [1, 2, 3]
    assert rows[0]["data"]["turn_id"] == "turn-1"
    assert rows[0]["session_id"] == "sess-1"
    assert rows[1]["data"]["text"] == "从直觉讲起"
    # 引用锚点 = user_message 行；水位不动（没触发整合）
    month = time.strftime("%Y-%m", time.localtime())
    assert service.turn_ref("turn-1") == ("chat", f"{month}.jsonl", 1)
    assert service.surface_for_turn("turn-1") == "chat"
    assert service.state.watermark("chat") == {"file": "", "line": 0}
    assert service.state.turns_since() == 1


async def test_record_turn_without_begin_is_noop(tmp_path):
    service = MemoryService(tmp_path)
    bus = StreamBus("turn-x", session_id="sess-1")
    await bus.emit_done(response="答案", status="completed")
    await service.record_turn("turn-x", "sess-1", bus.history)

    assert _trace_rows(tmp_path) == []
    assert service.state.turns_since() == 0  # 没落痕就不计数（不猜 surface）


async def test_trace_disabled_skips_collection(tmp_path, monkeypatch):
    service = MemoryService(tmp_path)
    monkeypatch.setattr(service, "config", lambda: MemoryConfig(trace_enabled=False))
    await _record(service, "turn-1")

    assert _trace_rows(tmp_path) == []
    assert service.turn_ref("turn-1") is None
    assert service.state.turns_since() == 0


async def test_turn_registry_cap_evicts_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_service_module, "TURN_REGISTRY_CAP", 3)
    service = MemoryService(tmp_path)
    for index in range(4):
        await _record(service, f"turn-{index}")

    assert service.surface_for_turn("turn-0") is None  # 最旧被挤掉
    assert service.surface_for_turn("turn-3") == "chat"


async def test_auto_threshold_spawns_background_consolidation(tmp_path, monkeypatch):
    service = MemoryService(tmp_path)
    config = MemoryConfig(auto_threshold_turns=2, budget_update=0, budget_audit=0, budget_dedup=0)
    monkeypatch.setattr(service, "config", lambda: config)

    await _record(service, "turn-1")
    assert service.state.last_run() is None  # 没到阈值：不动
    await _record(service, "turn-2")

    deadline = time.monotonic() + 3.0
    last = service.state.last_run()
    while time.monotonic() < deadline:
        last = service.state.last_run()
        if last is not None and last["status"] != "running":
            break
        await asyncio.sleep(0.01)
    assert last is not None and last["trigger"] == "auto" and last["status"] == "ok"
    assert service.state.turns_since() == 0  # 整合成功重置计数


async def test_consolidate_single_flight(tmp_path, monkeypatch):
    service = MemoryService(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GateConsolidator:
        def __init__(self, **kwargs) -> None:
            pass

        async def run_consolidation(self, **kwargs) -> ConsolidationRun:
            entered.set()
            await release.wait()
            return ConsolidationRun(
                id="mrun-11111111", trigger=kwargs.get("trigger", "manual"), status="ok"
            )

    monkeypatch.setattr(memory_service_module, "Consolidator", _GateConsolidator)

    task = asyncio.create_task(service.consolidate(trigger="manual"))
    await asyncio.wait_for(entered.wait(), 1.0)
    assert await service.consolidate(trigger="manual") is None  # 撞锁：手动 → 409
    assert await service.consolidate(trigger="auto") is None  # 自动 → 静默跳过
    release.set()
    run = await task
    assert run is not None and run.status == "ok"


async def test_recover_interrupted(tmp_path):
    service = MemoryService(tmp_path)
    assert service.recover_interrupted() is False
    service.state.set_last_run({"id": "mrun-22222222", "trigger": "auto", "status": "running"})

    assert service.recover_interrupted() is True
    last = service.state.last_run()
    assert last is not None and last["status"] == "interrupted"
    assert service.recover_interrupted() is False  # 幂等


async def test_shutdown_cancels_background_tasks(tmp_path):
    service = MemoryService(tmp_path)

    async def _hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_hang())
    service._tasks.add(task)
    await service.shutdown()
    assert task.cancelled()
    assert service._tasks == set()


async def test_app_lifespan_wires_memory_service(tmp_home):
    from nnnu.api.main import create_app

    set_memory_service(None)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.memory, MemoryService)
        assert get_memory_service() is app.state.memory
    assert get_memory_service() is None  # teardown 摘干净
