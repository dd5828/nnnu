"""consolidator 单测：update 增量 / audit 引用对账 / dedup 收敛 / 预算与重放。

LLM 一律脚本化（install_scripted 全局接管 create_client），零网络。需要「按本
chunk 引用池出事实」的用例用 _PoolLLM：动态从提示词里抠引用池，比静态脚本更贴
真实调用序（切块数事先不知道）。
"""

import json
import re
import time

import pytest

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMRequest, LLMResponse
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.memory import trace
from nnnu.services.memory.consolidator import audit as audit_mode
from nnnu.services.memory.consolidator import dedup as dedup_mode
from nnnu.services.memory.consolidator import update as update_mode
from nnnu.services.memory.consolidator.chunk import chunk_units
from nnnu.services.memory.consolidator.guards import find_banned
from nnnu.services.memory.consolidator.pipeline import Consolidator
from nnnu.services.memory.models import MemoryConfig, MemoryDoc, MemoryEntry, text_digest
from nnnu.services.memory.state import StateStore
from nnnu.services.memory.store import MemoryStore

pytestmark = pytest.mark.usefixtures("tmp_home", "repo_prompts")


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _sept(day: int = 15) -> float:
    return time.mktime((2026, 9, day, 12, 0, 0, 0, 0, -1))


def _make(tmp_path, **kwargs) -> Consolidator:
    return Consolidator(
        data_root=tmp_path,
        store=MemoryStore(tmp_path),
        state=StateStore(tmp_path),
        config=kwargs.pop("config", MemoryConfig()),
        lang=kwargs.pop("lang", "zh"),
    )


async def _seed_turns(tmp_path, n: int = 2, *, surface: str = "chat", pad: int = 0) -> int:
    """造 n 个回合（user_message + assistant_done），返回行数。"""
    writer = trace.TraceWriter(tmp_path)
    total = 0
    for i in range(n):
        turn = f"turn-{i}"
        base = _sept() + i
        user = trace.user_message_row(
            surface=surface, session_id="s", turn_id=turn, text=f"用户消息{i}" + "字" * pad
        )
        user["ts"] = base
        done = {
            "ts": base + 1,
            "surface": surface,
            "session_id": "s",
            "event": "assistant_done",
            "data": {"turn_id": turn, "status": "completed", "text": f"回答{i}", "chars": 3},
        }
        total += len(await writer.append(surface, [user, done]))
    return total


async def _add_l2_entry(
    host: Consolidator, text: str, refs: list[str], entry_id: str
) -> MemoryEntry:
    entry = MemoryEntry(
        id=entry_id, date="2026-09-18", text=text, refs=refs, layer="l2", key="chat"
    )
    await host.store.append_entries("l2", "chat", [entry])
    host.state.register_entry("L2/chat.md", entry_id, text_digest(text), "consolidator")
    return entry


class _PoolLLM:
    """动态替身：同一句文本 + 本 chunk 第一个引用；记录每次请求。"""

    def __init__(self, text: str = "用户在做信号处理方向的学习") -> None:
        self.text = text
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        user = str(request.messages[-1].get("content", ""))
        refs = re.findall(r"\[([a-z0-9_]+/\d{4}-\d{2}\.jsonl#\d+)\]", user)
        return LLMResponse(text=_json({"facts": [{"text": self.text, "refs": refs[:1]}]}))


class _EmptyFactsLLM:
    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(text=_json({"facts": []}))


# ---- 纯函数：切块与守卫 ----


def test_chunk_units_splits_long_unit_and_keeps_refs():
    long_text = "".join(f"第{i}句内容。" for i in range(40))
    chunks = chunk_units([("chat/2026-09.jsonl#1", long_text)], 100)
    assert len(chunks) >= 3
    assert all(ref == "chat/2026-09.jsonl#1" for chunk in chunks for ref, _ in chunk)
    assert all(sum(len(text) for _, text in chunk) <= 110 for chunk in chunks)  # 预算 + 10% 重叠


def test_chunk_units_overlaps_previous_tail():
    units = [(f"chat/2026-09.jsonl#{i}", f"词汇内容字{i:02d}") for i in range(1, 16)]  # 每个 7 字
    chunks = chunk_units(units, 100)
    assert len(chunks) == 2
    # 第二块开头重复上一块末尾的单元（10% 重叠预算内）
    assert chunks[1][0] == chunks[0][-1]


def test_guards_banned_with_quote_exemption():
    assert find_banned("用户总是跳过推导过程") == "总是"
    assert find_banned("用户说过「我从来不背公式」") is None
    assert find_banned("the user NEVER reads docs") == "never"
    assert find_banned("用户偏爱从例子入手") is None


# ---- update：L2 ----


async def test_update_writes_facts_with_pool_refs(tmp_path):
    host = _make(tmp_path)
    total = await _seed_turns(tmp_path, n=2)
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "facts": [
                                {
                                    "text": "用户在做信号处理方向的学习",
                                    "refs": ["chat/2026-09.jsonl#1"],
                                }
                            ]
                        }
                    )
                ]
            )
        ]
    )
    install_scripted(lambda: scripted)
    await update_mode.update_surface(host, "chat")

    doc = host.store.load("l2", "chat")
    assert [entry.text for entry in doc.entries] == ["用户在做信号处理方向的学习"]
    entry = doc.entries[0]
    assert entry.id is not None and entry.id.startswith("mem-")
    assert entry.refs == ["L1:chat/2026-09.jsonl#1"]
    assert entry.date == "2026-09-15"  # 取来源事件那天
    assert host.state.entry_meta("L2/chat.md", entry.id) is not None
    assert host.state.watermark("chat") == {"file": "2026-09.jsonl", "line": total}
    assert trace.resolve_ref(tmp_path, "chat/2026-09.jsonl#1") is not None


async def test_update_rejects_bad_facts(tmp_path):
    host = _make(tmp_path)
    await _seed_turns(tmp_path, n=1)
    filler = ScriptedStep(chunks=[_json({"facts": []})])  # update_l3 那一步
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "facts": [
                                {"text": "池外引用的条目", "refs": ["chat/2026-01.jsonl#9"]},
                                {"text": "没有引用的条目", "refs": []},
                                {"text": "用户总是跳过推导", "refs": ["chat/2026-09.jsonl#1"]},
                                {
                                    "text": "用户在做信号处理方向的学习",
                                    "refs": ["chat/2026-09.jsonl#1"],
                                },
                                {
                                    "text": "用户在做信号处理方向的学习",
                                    "refs": ["chat/2026-09.jsonl#2"],
                                },
                            ]
                        }
                    )
                ]
            ),
            filler,
        ]
    )
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])

    doc = host.store.load("l2", "chat")
    assert [entry.text for entry in doc.entries] == ["用户在做信号处理方向的学习"]  # 批内去重
    # 池外引用记 2 次（坏引用 + 条目因此无有效引用）、无引用条目记 1 次
    assert run.stats.get("ref_rejected") == 3
    assert run.stats.get("banned_dropped") == 1
    assert run.status == "ok"


async def test_update_watermark_advances_per_chunk_and_budget_stops(tmp_path):
    host = _make(tmp_path, config=MemoryConfig(budget_update=1, update_chunk_chars=500))
    total = await _seed_turns(tmp_path, n=6, pad=120)
    llm = _PoolLLM()
    install_scripted(lambda: llm)

    first = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])
    assert first.status == "ok"
    cursor = host.state.watermark("chat")
    assert 0 < cursor["line"] < total  # 预算 1：只吃下第一块，水位停在中途
    assert len(llm.calls) == 1
    assert len(host.store.load("l2", "chat").entries) == 1

    runs = 1
    while host.state.watermark("chat")["line"] < total:
        run = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])
        assert run.status == "ok"
        runs += 1
        assert runs < 10  # 防死循环：块数有限，几轮内必然吃完
    assert len(llm.calls) >= 2  # 多块确凿：分了几块就吃了几轮
    # 同一句事实 + 重叠块重放：归一化去重兜住，不重复入库
    assert len(host.store.load("l2", "chat").entries) == 1


async def test_update_watermark_untouched_when_budget_zero(tmp_path):
    host = _make(tmp_path, config=MemoryConfig(budget_update=0))
    await _seed_turns(tmp_path, n=2)
    scripted = ScriptedLLM([])  # 一旦调用就抛「脚本耗尽」
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])
    assert run.status == "ok"
    assert host.state.watermark("chat") == {"file": "", "line": 0}
    assert host.store.load("l2", "chat").entries == []


# ---- update：L3 ----


async def test_update_l3_synthesizes_from_l2_entries(tmp_path):
    host = _make(tmp_path)
    entry = await _add_l2_entry(
        host, "用户偏好从例子入手学习", ["L1:chat/2026-09.jsonl#1"], "mem-11111111"
    )
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "facts": [
                                {
                                    "doc": "profile",
                                    "text": "偏好事例先行的学习方式",
                                    "refs": [f"chat#{entry.id}"],
                                },
                                {
                                    "doc": "unknown-doc",
                                    "text": "归档名非法的条目",
                                    "refs": [f"chat#{entry.id}"],
                                },
                            ]
                        }
                    )
                ]
            )
        ]
    )
    install_scripted(lambda: scripted)
    await update_mode.update_l3(host, ("chat",))

    profile = host.store.load("l3", "profile")
    assert [item.text for item in profile.entries] == ["偏好事例先行的学习方式"]
    assert profile.entries[0].refs == [f"L2:chat#{entry.id}"]
    assert host.state.l3_seen("chat") == {entry.id: text_digest(entry.text)}

    # 条目已消费：再跑一轮不再调 LLM（脚本耗尽会当场炸）
    await update_mode.update_l3(host, ("chat",))


# ---- audit：引用对账 ----


async def test_audit_marks_stale_and_recovers(tmp_path):
    host = _make(tmp_path)
    await _add_l2_entry(host, "引用还没出现的条目", ["L1:chat/2026-09.jsonl#9"], "mem-11111111")
    await _add_l2_entry(host, "引用被删条目的", ["L2:chat#mem-22222222"], "mem-33333333")
    host.state.add_tombstone("mem-22222222", "l2", "chat.md")

    await audit_mode.deterministic_audit(host, "l2", "chat")
    doc = host.store.load("l2", "chat")
    assert [entry.stale for entry in doc.entries] == [True, True]

    # 造出第 9 行后，第一条自动摘掉 STALE（第二条目标仍被 tombstone）
    writer = trace.TraceWriter(tmp_path)
    rows = []
    for i in range(9):
        row = trace.user_message_row(surface="chat", session_id="s", turn_id="t", text=f"第{i}行")
        row["ts"] = _sept() + i
        rows.append(row)
    await writer.append("chat", rows)

    await audit_mode.deterministic_audit(host, "l2", "chat")
    doc = host.store.load("l2", "chat")
    assert [entry.stale for entry in doc.entries] == [False, True]


async def test_audit_assigns_ids_to_anonymous_entries(tmp_path):
    host = _make(tmp_path)
    doc = MemoryDoc(layer="l2", key="chat")
    doc.entries.append(
        MemoryEntry(
            id=None, date="2026-09-18", text="手写的匿名条目", refs=[], layer="l2", key="chat"
        )
    )
    host.store.save("l2", "chat", doc)

    await audit_mode.deterministic_audit(host, "l2", "chat")
    entry = host.store.load("l2", "chat").entries[0]
    assert entry.id is not None and entry.id.startswith("mem-")
    meta = host.state.entry_meta("L2/chat.md", entry.id)
    assert meta is not None and meta["edited"] is True and meta["origin"] == "human"


async def test_audit_revision_applies_reverse_order(tmp_path):
    host = _make(tmp_path)
    doc = MemoryDoc(layer="l2", key="chat")
    for index, entry_id in enumerate(("mem-11111111", "mem-22222222", "mem-33333333")):
        doc.entries.append(
            MemoryEntry(
                id=entry_id,
                date="2026-09-18",
                text=f"第{index}条",
                refs=["L1:chat/2026-09.jsonl#1"],
                layer="l2",
                key="chat",
            )
        )
    host.store.save("l2", "chat", doc)
    # 造出被引用的第 1 行
    writer = trace.TraceWriter(tmp_path)
    row = trace.user_message_row(surface="chat", session_id="s", turn_id="t", text="证据行")
    row["ts"] = _sept()
    await writer.append("chat", [row])
    for entry in doc.entries:
        host.state.register_entry(
            "L2/chat.md", entry.id or "", text_digest(entry.text), "consolidator"
        )
    middle = doc.entries[1]
    host.state.mark_edited("L2/chat.md", middle.id or "", text_digest(middle.text))

    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "edits": [
                                {"line": 3, "text": "第0条改"},
                                {"line": 4, "text": "第1条被保护不该动"},
                                {"line": 5, "text": "第2条改"},
                            ]
                        }
                    )
                ]
            )
        ]
    )
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["audit"], surfaces=["chat"])

    texts = [entry.text for entry in host.store.load("l2", "chat").entries]
    assert texts == ["第0条改", "第1条", "第2条改"]
    assert run.stats.get("skipped_protected") == 1
    # 修订过的条目哈希已刷新（下次不再被当成带外编辑）
    first = host.store.load("l2", "chat").entries[0]
    meta = host.state.entry_meta("L2/chat.md", first.id or "")
    assert meta is not None and meta["hash"] == text_digest("第0条改")


# ---- dedup ----


async def test_dedup_merges_converges_and_rejects_insert(tmp_path):
    host = _make(tmp_path)
    doc = MemoryDoc(layer="l2", key="chat")
    # 前两条二元组 Jaccard ≥ 0.8（粗筛能收进 dedup 范围），第三条无关
    for entry_id, text in (
        ("mem-11111111", "用户偏好先看结论再看推导"),
        ("mem-22222222", "用户偏好先看结论再看推导过程"),
        ("mem-44444444", "用户在准备考研数学"),
    ):
        doc.entries.append(
            MemoryEntry(id=entry_id, date="2026-09-18", text=text, refs=[], layer="l2", key="chat")
        )
    host.store.save("l2", "chat", doc)
    for entry in doc.entries:
        host.state.register_entry(
            "L2/chat.md", entry.id or "", text_digest(entry.text), "consolidator"
        )

    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "ops": [
                                {
                                    "op": "replace",
                                    "line": 4,
                                    "text": "用户偏好先看结论，再看推导过程",
                                },
                                {"op": "delete", "line": 3},
                                {"op": "insert", "line": 3, "text": "不该新增的条目"},
                            ]
                        }
                    )
                ]
            ),
            ScriptedStep(chunks=[_json({"ops": []})]),  # 第二轮：收敛，提前停
        ]
    )
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["dedup"], surfaces=["chat"])

    doc = host.store.load("l2", "chat")
    assert [entry.text for entry in doc.entries] == [
        "用户偏好先看结论，再看推导过程",
        "用户在准备考研数学",
    ]
    assert doc.entries[0].id == "mem-22222222"
    assert run.stats.get("dedup_rejected") == 1  # insert 被拒
    assert "mem-11111111" in host.state.tombstone_ids()
    assert scripted.exhausted  # 空 ops 收敛：两步都跑了


def test_dedup_suspect_prefilter():
    def entry(text: str) -> MemoryEntry:
        return MemoryEntry(id="mem-11111111", date="2026-09-18", text=text, refs=[], layer="l2")

    near = [entry("用户偏好先看结论再看推导"), entry("用户偏好先看结论再看推导过程")]
    far = [entry("用户在做信号处理学习"), entry("用户最近在看强化学习")]
    assert dedup_mode.suspect_duplicates(near)
    assert not dedup_mode.suspect_duplicates(far)


# ---- 运行账与失败恢复 ----


async def test_run_records_last_run_and_resets_turns(tmp_path):
    host = _make(tmp_path)
    await _seed_turns(tmp_path, n=1)
    host.state.bump_turn()
    host.state.bump_turn()
    llm = _EmptyFactsLLM()
    install_scripted(lambda: llm)

    run = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])
    assert run.status == "ok"
    assert host.state.turns_since() == 0
    last = host.state.last_run()
    assert last is not None and last["status"] == "ok" and last["id"] == run.id
    assert last["stats"]["events"] == 2  # 一个回合两行
    assert last["stats"]["llm_calls"] == 1
    assert last["finished_at"] is not None


async def test_run_marks_error_without_resetting_turns(tmp_path):
    host = _make(tmp_path)
    await _seed_turns(tmp_path, n=1)
    host.state.bump_turn()
    scripted = ScriptedLLM([])  # 任何 LLM 调用都会炸
    install_scripted(lambda: scripted)

    run = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])
    assert run.status == "error"
    assert run.error and "RuntimeError" in run.error
    assert host.state.turns_since() == 1  # 失败不重置
    last = host.state.last_run()
    assert last is not None and last["status"] == "error"
