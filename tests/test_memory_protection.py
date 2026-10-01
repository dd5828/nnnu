"""人工编辑保护（§7.10 验收③）：整合永远不覆盖人工改过的 L2/L3 条目。

四条防线各测一条：
1. update 只 append，天然不碰既有条目（哪怕模型想改）；
2. audit/dedup 看保护位：edited=true 的条目排除在可改集外；
3. 带外直接改 .md（工作台之外）→ 哈希对不上 → 视同人工编辑并登记；
4. PATCH→整合 往返：手改 + 哈希同步 + edited=true 之后跑全量整合，改不动；
   「交还自动管理」后整合又能改回来。
"""

import json
import time

import pytest

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.memory import trace
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


def _make(tmp_path) -> Consolidator:
    return Consolidator(
        data_root=tmp_path,
        store=MemoryStore(tmp_path),
        state=StateStore(tmp_path),
        config=MemoryConfig(),
        lang="zh",
    )


async def _seed_one_turn(tmp_path) -> int:
    writer = trace.TraceWriter(tmp_path)
    user = trace.user_message_row(
        surface="chat", session_id="s", turn_id="turn-1", text="帮我把傅里叶变换讲清楚"
    )
    user["ts"] = _sept()
    done = {
        "ts": _sept() + 1,
        "surface": "chat",
        "session_id": "s",
        "event": "assistant_done",
        "data": {"turn_id": "turn-1", "status": "completed", "text": "从直觉讲起", "chars": 5},
    }
    return len(await writer.append("chat", [user, done]))


async def _seed_managed_entry(host: Consolidator, text: str, entry_id: str = "mem-11111111"):
    entry = MemoryEntry(
        id=entry_id,
        date="2026-09-18",
        text=text,
        refs=["L1:chat/2026-09.jsonl#1"],
        layer="l2",
        key="chat",
    )
    await host.store.append_entries("l2", "chat", [entry])
    host.state.register_entry("L2/chat.md", entry_id, text_digest(text), "consolidator")
    return entry


# ---- ① update 只 append ----


async def test_update_appends_and_never_edits_existing(tmp_path):
    host = _make(tmp_path)
    await _seed_one_turn(tmp_path)
    await _seed_managed_entry(host, "用户偏好先看结论")
    # 手改既有条目正文（工作台之外，state 哈希没跟上——update 也该无感）
    await host.store.update_entry("l2", "chat", "mem-11111111", text="用户偏好先看结论（手改）")

    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "facts": [
                                {
                                    "text": "用户在学信号处理",
                                    "refs": ["chat/2026-09.jsonl#2"],
                                }
                            ]
                        }
                    )
                ]
            ),
            ScriptedStep(chunks=[_json({"facts": []})]),  # update_l3 那一步
        ]
    )
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["update"], surfaces=["chat"])

    assert run.status == "ok"
    entries = host.store.load("l2", "chat").entries
    assert entries[0].id == "mem-11111111"
    assert entries[0].text == "用户偏好先看结论（手改）"  # 一个字都没被动
    assert [entry.text for entry in entries[1:]] == ["用户在学信号处理"]


# ---- ② 带外编辑检测 ----


async def test_external_edit_detected_blocks_audit(tmp_path):
    host = _make(tmp_path)
    await _seed_one_turn(tmp_path)
    doc = MemoryDoc(layer="l2", key="chat")
    doc.entries.append(
        MemoryEntry(
            id="mem-11111111", date="2026-09-18", text="旧文本", refs=[], layer="l2", key="chat"
        )
    )
    host.store.save("l2", "chat", doc)
    host.state.register_entry("L2/chat.md", "mem-11111111", text_digest("旧文本"), "consolidator")
    # 绕过工作台直接改 .md：state 里的哈希还是「旧文本」的
    fresh = host.store.load("l2", "chat")
    fresh.entries[0].text = "被直接改过的文本"
    host.store.save("l2", "chat", fresh)

    scripted = ScriptedLLM([])  # 任何 LLM 调用都会炸——正好证明 rev 阶段没敢动它
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["audit"], surfaces=["chat"])

    assert run.status == "ok"
    assert scripted.calls == []
    assert host.store.load("l2", "chat").entries[0].text == "被直接改过的文本"
    meta = host.state.entry_meta("L2/chat.md", "mem-11111111")
    assert meta is not None and meta["edited"] is True and meta["origin"] == "human"
    assert meta["hash"] == text_digest("被直接改过的文本")
    assert run.stats.get("external_edits") == 1


# ---- ③ dedup 不删保护条目 ----


async def test_dedup_leaves_protected_duplicate_alone(tmp_path):
    host = _make(tmp_path)
    doc = MemoryDoc(layer="l2", key="chat")
    for entry_id, text in (
        ("mem-11111111", "用户偏好先看结论再看推导"),
        ("mem-22222222", "用户偏好先看结论，然后再看推导过程"),
    ):
        doc.entries.append(
            MemoryEntry(id=entry_id, date="2026-09-18", text=text, refs=[], layer="l2", key="chat")
        )
    host.store.save("l2", "chat", doc)
    host.state.register_entry(
        "L2/chat.md", "mem-11111111", text_digest("用户偏好先看结论再看推导"), "consolidator"
    )
    host.state.mark_edited(
        "L2/chat.md", "mem-22222222", text_digest("用户偏好先看结论，然后再看推导过程")
    )

    scripted = ScriptedLLM([])
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["dedup"], surfaces=["chat"])

    assert run.status == "ok"
    assert scripted.calls == []  # 可改条目只剩 1 条，压根没到 LLM
    assert len(host.store.load("l2", "chat").entries) == 2  # 两条都还在


# ---- ④ PATCH → 整合 往返 ----


async def test_patch_roundtrip_survives_full_run(tmp_path):
    host = _make(tmp_path)
    await _seed_one_turn(tmp_path)
    await _seed_managed_entry(host, "用户偏好事例教学")
    # 工作台 PATCH 的口径：写文件 + 哈希同步 + edited=true（origin=human）
    await host.store.update_entry("l2", "chat", "mem-11111111", text="用户偏好事例教学（我改的）")
    host.state.mark_edited("L2/chat.md", "mem-11111111", text_digest("用户偏好事例教学（我改的）"))

    scripted = ScriptedLLM(
        [
            # update 出一批新事件事实
            ScriptedStep(
                chunks=[
                    _json(
                        {"facts": [{"text": "用户在学信号处理", "refs": ["chat/2026-09.jsonl#2"]}]}
                    )
                ]
            ),
            # update_l3 无产出
            ScriptedStep(chunks=[_json({"facts": []})]),
            # audit 想让模型改第 3 行（=被保护的那条）
            ScriptedStep(chunks=[_json({"edits": [{"line": 3, "text": "模型试图改写"}]})]),
        ]
    )
    install_scripted(lambda: scripted)
    # 显式给模式：默认模式含 dedup/extract，多出的调用会偷吃脚本步骤
    run = await host.run_consolidation(trigger="manual", modes=["update", "audit", "dedup"])

    assert run.status == "ok"
    assert scripted.exhausted  # 三步都按预期被消费，没有多余调用
    entries = host.store.load("l2", "chat").entries
    assert entries[0].text == "用户偏好事例教学（我改的）"  # 手改原地保留
    assert entries[1].text == "用户在学信号处理"  # 新事实正常入库
    assert run.stats.get("skipped_protected") == 1  # 模型改保护行的 op 被丢
    meta = host.state.entry_meta("L2/chat.md", "mem-11111111")
    assert meta is not None and meta["edited"] is True and meta["origin"] == "human"


# ---- ⑤ 交还自动管理 ----


async def test_handback_allows_audit_revision(tmp_path):
    host = _make(tmp_path)
    await _seed_one_turn(tmp_path)
    await _seed_managed_entry(host, "旧措辞")
    host.state.mark_edited("L2/chat.md", "mem-11111111", text_digest("旧措辞"))
    # 「交还自动管理」：edited=false + 哈希对齐当前文本
    host.state.set_managed("L2/chat.md", "mem-11111111", text_digest("旧措辞"))

    scripted = ScriptedLLM(
        [ScriptedStep(chunks=[_json({"edits": [{"line": 3, "text": "新措辞"}]})])]
    )
    install_scripted(lambda: scripted)
    run = await host.run_consolidation(trigger="manual", modes=["audit"], surfaces=["chat"])

    assert run.status == "ok"
    assert host.store.load("l2", "chat").entries[0].text == "新措辞"
    meta = host.state.entry_meta("L2/chat.md", "mem-11111111")
    assert meta is not None
    assert meta["edited"] is False and meta["origin"] == "consolidator"
    assert meta["hash"] == text_digest("新措辞")
