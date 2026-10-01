"""L2/L3 存储与 state.json 单测：解析/渲染往返 / 条目级操作 / 锁 / 哈希账本。"""

import asyncio
import json

from nnnu.services.memory import paths
from nnnu.services.memory.models import MemoryEntry, text_digest
from nnnu.services.memory.state import StateStore, doc_key
from nnnu.services.memory.store import (
    MemoryStore,
    atomic_write_text,
    backup_file,
    default_header,
    parse_doc,
    parse_entry,
    render_doc,
    render_entry,
)


def _entry(text: str, *, refs: list[str] | None = None, **kwargs: object) -> MemoryEntry:
    base: dict = {
        "id": None,
        "date": "2026-09-18",
        "text": text,
        "refs": refs or [],
        "layer": "l2",
        "key": "chat",
    }
    base.update(kwargs)
    return MemoryEntry(**base)


# ---- 条目解析 / 渲染 ----


def test_entry_roundtrip_with_l1_refs():
    entry = _entry(
        "用户在做信号处理方向的学习",
        refs=["L1:chat/2026-09.jsonl#123", "L1:chat/2026-09.jsonl#124"],
        id="mem-1a2b3c4d",
    )
    line = render_entry(entry)
    assert line == (
        "- [2026-09-18] 用户在做信号处理方向的学习"
        " [L1:chat/2026-09.jsonl#123] [L1:chat/2026-09.jsonl#124] [mem-1a2b3c4d]"
    )
    parsed = parse_entry(line, layer="l2", key="chat")
    assert parsed is not None
    assert parsed.id == "mem-1a2b3c4d"
    assert parsed.text == entry.text
    assert parsed.refs == entry.refs


def test_entry_roundtrip_stale_and_anonymous():
    entry = _entry("用户偏好先看结论", stale=True, id=None)
    line = render_entry(entry)
    assert line.startswith("- [STALE] [2026-09-18] ")
    assert "[mem-" not in line
    parsed = parse_entry(line, layer="l2", key="chat")
    assert parsed is not None
    assert parsed.stale is True
    assert parsed.id is None


def test_l2_refs_packed_in_one_bracket():
    entry = _entry(
        "学习动机以工程落地为主",
        refs=["L2:chat#mem-11111111", "L2:deep_solve#mem-22222222"],
        layer="l3",
        key="profile",
        id="mem-33333333",
    )
    line = render_entry(entry)
    assert "[L2:chat#mem-11111111, deep_solve#mem-22222222]" in line
    parsed = parse_entry(line, layer="l3", key="profile")
    assert parsed is not None
    assert parsed.refs == ["L2:chat#mem-11111111", "L2:deep_solve#mem-22222222"]


def test_parse_entry_rejects_non_entry_lines():
    for line in ("# L2 · chat 分面事实", "", "手写的一段说明", "- [不是日期] 内容", "- "):
        assert parse_entry(line, layer="l2", key="chat") is None


def test_doc_roundtrip_header_entries_extras():
    text = (
        "# L2 · chat 分面事实\n"
        "\n"
        "- [2026-09-18] 第一条 [L1:chat/2026-09.jsonl#1] [mem-11111111]\n"
        "- [2026-09-18] 第二条 [L1:chat/2026-09.jsonl#2]\n"
        "用户手写的补充说明（机器别丢）\n"
    )
    doc = parse_doc(text, layer="l2", key="chat")
    assert doc.header == "# L2 · chat 分面事实"
    assert [entry.text for entry in doc.entries] == ["第一条", "第二条"]
    assert doc.extras == ["用户手写的补充说明（机器别丢）"]

    again = parse_doc(render_doc(doc), layer="l2", key="chat")
    assert [entry.text for entry in again.entries] == ["第一条", "第二条"]
    assert again.extras == doc.extras
    assert again.entries[0].id == "mem-11111111"


def test_default_header_used_when_missing():
    doc = parse_doc("- [2026-09-18] 只有条目的文档", layer="l2", key="chat")
    assert doc.header == default_header("l2", "chat")
    l3 = parse_doc("", layer="l3", key="profile")
    assert l3.header == "# L3 · 用户画像"
    assert render_doc(l3) == "# L3 · 用户画像\n"


# ---- MemoryStore ----


async def test_append_dedupes_by_normalized_text(tmp_path):
    store = MemoryStore(tmp_path)
    await store.append_entries("l2", "chat", [_entry("用户 偏好  从例子入手")])
    written = await store.append_entries(
        "l2",
        "chat",
        [_entry("用户 偏好 从例子入手"), _entry("新的一条"), _entry("新的一条")],
    )
    assert [entry.text for entry in written] == ["新的一条"]  # 批内也去重
    doc = store.load("l2", "chat")
    assert [entry.text for entry in doc.entries] == ["用户 偏好  从例子入手", "新的一条"]
    assert written[0].id.startswith("mem-")


async def test_update_and_delete_entry(tmp_path):
    store = MemoryStore(tmp_path)
    (added,) = await store.append_entries("l2", "chat", [_entry("原文本")])

    updated = await store.update_entry("l2", "chat", added.id, text="改过的文本")
    assert updated is not None and updated.text == "改过的文本"
    assert updated.id == added.id
    assert await store.update_entry("l2", "chat", "mem-deadbeef", text="x") is None

    deleted = await store.delete_entry("l2", "chat", added.id)
    assert deleted is not None and deleted.id == added.id
    assert store.load("l2", "chat").entries == []


async def test_find_locates_across_files(tmp_path):
    store = MemoryStore(tmp_path)
    (in_chat,) = await store.append_entries("l2", "chat", [_entry("chat 的")])
    (in_l3,) = await store.append_entries(
        "l3", "profile", [_entry("画像的", layer="l3", key="profile")]
    )
    found_key, found = store.find("l2", in_chat.id)
    assert found_key == "chat" and found.id == in_chat.id and found.text == in_chat.text
    found_key, found = store.find("l3", in_l3.id)
    assert found_key == "profile" and found.id == in_l3.id
    assert store.find("l2", "mem-00000000") is None


async def test_id_stable_after_deleting_middle_entry(tmp_path):
    store = MemoryStore(tmp_path)
    written = await store.append_entries("l2", "chat", [_entry("甲"), _entry("乙"), _entry("丙")])
    ids = [entry.id for entry in written]
    await store.delete_entry("l2", "chat", ids[1])
    doc = store.load("l2", "chat")
    assert [entry.id for entry in doc.entries] == [ids[0], ids[2]]  # id 不随位置漂
    assert doc.entries[1].line_no == doc.entries[0].line_no + 1  # 行号照实重钉


async def test_concurrent_append_no_lost_update(tmp_path):
    store = MemoryStore(tmp_path)
    await asyncio.gather(
        store.append_entries("l2", "chat", [_entry("甲的条目")]),
        store.append_entries("l2", "chat", [_entry("乙的条目")]),
    )
    texts = {entry.text for entry in store.load("l2", "chat").entries}
    assert texts == {"甲的条目", "乙的条目"}


def test_atomic_write_text_leaves_no_tmp(tmp_path):
    path = paths.l2_path(tmp_path, "chat")
    atomic_write_text(path, "内容一\n")
    atomic_write_text(path, "内容二\n")
    assert path.read_text(encoding="utf-8") == "内容二\n"
    assert list((tmp_path / "user" / "memory" / "L2").glob("*.tmp")) == []


def test_backup_file_copies_content(tmp_path):
    path = paths.l2_path(tmp_path, "chat")
    atomic_write_text(path, "原始内容\n")
    backup_file(path)
    assert path.with_name("chat.md.bak").read_text(encoding="utf-8") == "原始内容\n"


# ---- StateStore ----


def test_state_watermark_and_turns_persist(tmp_path):
    state = StateStore(tmp_path)
    assert state.watermark("chat") == {"file": "", "line": 0}
    assert state.bump_turn() == 1
    assert state.bump_turn() == 2
    state.set_watermark("chat", "2026-09.jsonl", 128)
    state.reset_turns()

    fresh = StateStore(tmp_path)
    assert fresh.watermark("chat") == {"file": "2026-09.jsonl", "line": 128}
    assert fresh.turns_since() == 0


def test_state_entry_meta_lifecycle(tmp_path):
    state = StateStore(tmp_path)
    key = doc_key("l2", "chat")
    state.register_entry(key, "mem-11111111", "sha256:a", "consolidator")
    meta = state.entry_meta(key, "mem-11111111")
    assert meta is not None and meta["edited"] is False

    state.mark_edited(key, "mem-11111111", "sha256:b")
    meta = state.entry_meta(key, "mem-11111111")
    assert meta is not None and meta["edited"] is True and meta["origin"] == "human"

    state.refresh_entry(key, "mem-11111111", "sha256:c", origin="consolidator")
    meta = state.entry_meta(key, "mem-11111111")
    assert meta is not None and meta["hash"] == "sha256:c" and meta["edited"] is True

    state.set_managed(key, "mem-11111111", "sha256:c")
    meta = state.entry_meta(key, "mem-11111111")
    assert meta is not None and meta["edited"] is False and meta["origin"] == "consolidator"

    state.drop_entry_meta(key, "mem-11111111")
    assert state.entry_meta(key, "mem-11111111") is None


def test_state_tombstones_cap_fifo(tmp_path):
    state = StateStore(tmp_path)
    for i in range(205):
        state.add_tombstone(f"mem-{i:08x}", "l2", "chat.md")
    ids = state.tombstone_ids()
    assert len(ids) == 200
    assert "mem-00000000" not in ids  # 最旧被挤出
    assert f"mem-{204:08x}" in ids


def test_state_last_run_and_interrupted(tmp_path):
    state = StateStore(tmp_path)
    assert state.last_run() is None
    state.set_last_run({"id": "mrun-11111111", "status": "running", "trigger": "manual"})
    assert state.mark_interrupted() is True
    run = state.last_run()
    assert run is not None and run["status"] == "interrupted"
    assert state.mark_interrupted() is False

    state.set_last_run({"id": "mrun-22222222", "status": "ok"})
    assert state.mark_interrupted() is False


def test_state_corrupt_file_falls_back(tmp_path):
    path = paths.state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    state = StateStore(tmp_path)
    assert state.watermark("chat") == {"file": "", "line": 0}
    assert state.turns_since() == 0
    assert state.last_run() is None


def test_state_l3_seen_roundtrip(tmp_path):
    state = StateStore(tmp_path)
    assert state.l3_seen("chat") == {}
    state.mark_l3_seen("chat", {"mem-11111111": "sha256:x"})
    assert StateStore(tmp_path).l3_seen("chat") == {"mem-11111111": "sha256:x"}


def test_text_digest_ignores_whitespace(tmp_path):
    assert text_digest("用户 偏好\n从例子入手") == text_digest("用户   偏好 从例子入手")
    assert text_digest("甲") != text_digest("乙")


def test_state_file_is_valid_json(tmp_path):
    state = StateStore(tmp_path)
    state.bump_turn()
    raw = json.loads(paths.state_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["turns_since_consolidation"] == 1
