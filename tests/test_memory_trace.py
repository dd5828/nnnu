"""L1 轨迹层单测：事件映射 / 4KB 构造保证 / 按月分片 / 并发 / 引用校验。"""

import asyncio
import json
import time

import pytest

from nnnu.core.events import StreamEvent, StreamEventType
from nnnu.runtime import bootstrap
from nnnu.runtime.registry.capability_registry import get_capability_registry
from nnnu.services.memory import paths, trace


def _ev(kind: StreamEventType, **payload: object) -> StreamEvent:
    return StreamEvent.make(kind, "turn-1", session_id="sess-1a2b3c4d", **payload)


def _sept(day: int = 15) -> float:
    return time.mktime((2026, 9, day, 12, 0, 0, 0, 0, -1))


def _oct(day: int = 15) -> float:
    return time.mktime((2026, 10, day, 12, 0, 0, 0, 0, -1))


# ---- rows_from_events：事件映射 ----


def test_rows_tool_call_pairs_result_by_call_id():
    events = [
        _ev(
            StreamEventType.TOOL_CALL,
            tool_name="read_file",
            args={"path": "a.md"},
            call_id="c1",
        ),
        _ev(StreamEventType.TOOL_RESULT, call_id="c1", ok=True, summary="读到了"),
        _ev(StreamEventType.TOOL_CALL, tool_name="write_file", args={}, call_id="c2"),
    ]
    rows = trace.rows_from_events(events, turn_id="turn-1", surface="chat", session_id="s")
    assert [row["event"] for row in rows] == ["tool_call", "tool_call"]
    paired, dangling = rows
    assert paired["data"]["ok"] is True
    assert paired["data"]["summary"] == "读到了"
    assert json.loads(paired["data"]["args_preview"]) == {"path": "a.md"}
    assert dangling["data"]["ok"] is None  # 没等到结果的悬挂调用


def test_rows_ask_user_pairs_reply_by_ask_id():
    events = [
        _ev(
            StreamEventType.ASK_USER,
            question="想先学哪块？",
            options=[{"label": "傅里叶", "description": "频域"}, {"label": "小波"}],
            ask_id="ask-1",
        ),
        _ev(StreamEventType.ASK_USER_REPLY, ask_id="ask-1", answer="傅里叶"),
        _ev(StreamEventType.ASK_USER, question="还没答的问题", options=[], ask_id="ask-2"),
    ]
    rows = trace.rows_from_events(events, turn_id="turn-1", surface="chat", session_id="s")
    answered, pending = rows
    assert answered["data"]["answered"] is True
    assert answered["data"]["answer"] == "傅里叶"
    assert answered["data"]["options"] == ["傅里叶", "小波"]
    assert pending["data"]["answered"] is False
    assert pending["data"]["answer"] == ""


def test_rows_done_prefers_response_and_falls_back_to_content():
    events = [
        _ev(StreamEventType.CONTENT_DONE, full_text="全文（流式收尾）"),
        _ev(StreamEventType.DONE, response="", status="completed"),
    ]
    rows = trace.rows_from_events(events, turn_id="turn-1", surface="chat", session_id="s")
    assert rows[0]["event"] == "assistant_done"
    assert rows[0]["data"]["text"] == "全文（流式收尾）"


def test_rows_stopped_without_done_records_stopped():
    events = [
        _ev(StreamEventType.CONTENT_DONE, full_text="写到一半的内容"),
        _ev(StreamEventType.STOPPED),
    ]
    rows = trace.rows_from_events(events, turn_id="turn-1", surface="chat", session_id="s")
    assert rows[0]["data"]["status"] == "stopped"
    assert rows[0]["data"]["text"] == "写到一半的内容"


def test_rows_error_without_terminal_records_failed():
    events = [_ev(StreamEventType.ERROR, message="模型超时")]
    rows = trace.rows_from_events(events, turn_id="turn-1", surface="chat", session_id="s")
    assert rows[0]["data"]["status"] == "failed"


def test_rows_cost_keeps_top5_and_bundles_others():
    per_model = {
        f"m{i}": {"provider": "p", "input_tokens": 10, "output_tokens": 5, "cost": float(i)}
        for i in range(7)
    }
    events = [_ev(StreamEventType.COST_SUMMARY, tokens=105, cost=21.0, per_model=per_model)]
    rows = trace.rows_from_events(events, turn_id="turn-1", surface="chat", session_id="s")
    data = rows[0]["data"]
    assert len(data["per_model"]) == 6  # 前 5 + others
    assert "m6" in data["per_model"] and "m0" not in data["per_model"]
    assert data["per_model"]["others"]["cost"] == pytest.approx(0.0 + 1.0)
    assert data["per_model"]["m6"]["tokens"] == 15


def test_user_message_row_truncates_at_limit():
    row = trace.user_message_row(surface="chat", session_id="s", turn_id="turn-1", text="字" * 1500)
    assert row["event"] == "user_message"
    assert len(row["data"]["text"]) == trace.MAX_USER_CHARS
    assert row["data"]["chars"] == 1500
    assert row["data"]["truncated"] is True


# ---- fit_line：4KB 构造保证 ----


def test_fit_line_hits_byte_budget_for_chinese():
    record = {
        "ts": 1.0,
        "surface": "chat",
        "session_id": "s",
        "event": "user_message",
        "data": {"turn_id": "turn-1", "text": "汉" * 3000},
    }
    line = trace.fit_line(record)
    assert len(line) <= trace.MAX_LINE_BYTES
    parsed = json.loads(line.decode("utf-8"))
    assert parsed["data"]["truncated"] is True
    assert len(parsed["data"]["text"]) < 3000


def test_fit_line_raises_when_nothing_shrinkable():
    record = {
        "ts": 1.0,
        "surface": "chat",
        "session_id": "s",
        "event": "tool_call",
        "data": {"turn_id": "turn-1", "tool_name": "x" * 5000},
    }
    with pytest.raises(ValueError):
        trace.fit_line(record)


def test_fit_line_keeps_normal_row_intact():
    record = {
        "ts": 1.0,
        "surface": "chat",
        "session_id": "s",
        "event": "user_message",
        "data": {"turn_id": "turn-1", "seq": 1, "text": "你好"},
    }
    parsed = json.loads(trace.fit_line(record).decode("utf-8"))
    assert parsed == record


# ---- TraceWriter：分片 / seq / 并发 ----


async def test_append_monthly_shards_and_seq(tmp_path):
    writer = trace.TraceWriter(tmp_path)
    row_sept = trace.user_message_row(surface="chat", session_id="s", turn_id="t1", text="九月")
    row_sept["ts"] = _sept()
    row_oct = trace.user_message_row(surface="chat", session_id="s", turn_id="t2", text="十月")
    row_oct["ts"] = _oct()

    positions = await writer.append("chat", [row_sept])
    positions += await writer.append("chat", [row_oct])
    assert positions == [("2026-09.jsonl", 1), ("2026-10.jsonl", 1)]

    _, rows = trace.read_page(tmp_path, "chat", "2026-09.jsonl")
    assert rows[0]["data"]["seq"] == 1
    assert paths.trace_path(tmp_path, "chat", "2026-09").exists()


async def test_append_rejects_bad_surface(tmp_path):
    writer = trace.TraceWriter(tmp_path)
    row = trace.user_message_row(surface="chat", session_id="s", turn_id="t1", text="x")
    with pytest.raises(ValueError):
        await writer.append("bad/surface", [row])


async def test_concurrent_append_no_interleave(tmp_path):
    writer = trace.TraceWriter(tmp_path)
    rows_a = []
    rows_b = []
    for i in range(5):
        row_a = trace.user_message_row(
            surface="chat", session_id="s", turn_id=f"ta{i}", text=f"a{i}"
        )
        row_a["ts"] = _sept()
        rows_a.append(row_a)
        row_b = trace.user_message_row(
            surface="chat", session_id="s", turn_id=f"tb{i}", text=f"b{i}"
        )
        row_b["ts"] = _sept()
        rows_b.append(row_b)
    results = await asyncio.gather(writer.append("chat", rows_a), writer.append("chat", rows_b))
    all_positions = sorted(line for batch in results for _, line in batch)
    assert all_positions == list(range(1, 11))
    total, rows = trace.read_page(tmp_path, "chat", "2026-09.jsonl", limit=100)
    assert total == 10
    assert [row["data"]["seq"] for row in rows] == list(range(1, 11))


async def test_read_since_cursor_crosses_files(tmp_path):
    writer = trace.TraceWriter(tmp_path)
    for i in range(3):
        row = trace.user_message_row(surface="chat", session_id="s", turn_id=f"t{i}", text=f"s{i}")
        row["ts"] = _sept(i + 1)
        await writer.append("chat", [row])
    for i in range(2):
        row = trace.user_message_row(surface="chat", session_id="s", turn_id=f"u{i}", text=f"o{i}")
        row["ts"] = _oct(i + 1)
        await writer.append("chat", [row])

    everything = trace.read_since(tmp_path, "chat", None)
    assert [(name, line) for name, line, _ in everything] == [
        ("2026-09.jsonl", 1),
        ("2026-09.jsonl", 2),
        ("2026-09.jsonl", 3),
        ("2026-10.jsonl", 1),
        ("2026-10.jsonl", 2),
    ]
    backlog = trace.read_since(tmp_path, "chat", {"file": "2026-09.jsonl", "line": 1})
    assert [(name, line) for name, line, _ in backlog] == [
        ("2026-09.jsonl", 2),
        ("2026-09.jsonl", 3),
        ("2026-10.jsonl", 1),
        ("2026-10.jsonl", 2),
    ]
    assert trace.read_since(tmp_path, "chat", {"file": "2026-10.jsonl", "line": 2}) == []


# ---- 引用解析与校验 ----


async def test_resolve_ref_roundtrip_and_tamper(tmp_path):
    writer = trace.TraceWriter(tmp_path)
    row = trace.user_message_row(surface="chat", session_id="s", turn_id="t1", text="被引用行")
    row["ts"] = _sept()
    ((filename, line),) = await writer.append("chat", [row])

    resolved = trace.resolve_ref(tmp_path, f"chat/{filename}#{line}")
    assert resolved is not None
    assert resolved["data"]["text"] == "被引用行"

    # 带外编辑：把第 1 行换成 seq 对不上的行 → 引用判失效
    path = paths.trace_path(tmp_path, "chat", "2026-09")
    path.write_text(
        json.dumps(
            {
                "ts": 1.0,
                "surface": "chat",
                "session_id": "s",
                "event": "user_message",
                "data": {"seq": 9, "text": "手改过"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert trace.resolve_ref(tmp_path, f"chat/{filename}#{line}") is None


def test_parse_ref_rejects_traversal():
    assert trace.parse_ref("chat/../../secret#1") is None
    assert trace.parse_ref("chat/2026-09.jsonl#0") is None
    assert trace.parse_ref("chat/2026-09.jsonl#12") == ("chat", "2026-09.jsonl", 12)


# ---- 读取辅助：分页 / 统计 / surface 锁 ----


async def test_read_page_offset_and_file_stats(tmp_path):
    writer = trace.TraceWriter(tmp_path)
    rows = []
    for text in ("一", "二", "三"):
        row = trace.user_message_row(surface="chat", session_id="s", turn_id="t1", text=text)
        row["ts"] = _sept()
        rows.append(row)
    await writer.append("chat", rows)

    total, page = trace.read_page(tmp_path, "chat", "2026-09.jsonl", offset=1, limit=1)
    assert total == 3
    assert [row["data"]["text"] for row in page] == ["二"]
    assert trace.read_page(tmp_path, "chat", "../escape.jsonl") == (0, [])

    stats = trace.file_stats(paths.trace_path(tmp_path, "chat", "2026-09"))
    assert stats["lines"] == 3
    assert stats["events"] == {"user_message": 3}
    assert stats["bytes"] > 0


def test_surfaces_align_with_capability_manifests():
    bootstrap.register_builtins()
    names = {manifest.name for manifest in get_capability_registry().manifests()}
    assert set(paths.SURFACES) == names
