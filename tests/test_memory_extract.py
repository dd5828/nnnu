"""extract 语义抽取单测（§7.10 补做）：短路顺序 / 落盘 / 校验丢弃 / 跨块合并 / stale。

LLM 一律脚本化（install_scripted 全局接管 create_client），零网络。短路顺序是本模块的
契约：预算 0 → 无条目 → 源哈希未变，三步都不烧 LLM；`ask_json` 返回 None（解析失败）
→ 整轮不落盘，下轮重试（宁可整份重来，不落半份工件）。
"""

import json

import pytest

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.memory import paths, semantic
from nnnu.services.memory.consolidator import extract as extract_mode
from nnnu.services.memory.consolidator.pipeline import Consolidator
from nnnu.services.memory.models import MemoryConfig, MemoryEntry, text_digest
from nnnu.services.memory.state import StateStore
from nnnu.services.memory.store import MemoryStore
from nnnu.services.settings.atomic import atomic_write_json

pytestmark = pytest.mark.usefixtures("tmp_home", "repo_prompts")


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _make(tmp_path, **kwargs) -> Consolidator:
    return Consolidator(
        data_root=tmp_path,
        store=MemoryStore(tmp_path),
        state=StateStore(tmp_path),
        config=kwargs.pop("config", MemoryConfig()),
        lang=kwargs.pop("lang", "zh"),
    )


async def _seed_l2(
    host: Consolidator, text: str, entry_id: str, *, date: str = "2026-09-18"
) -> MemoryEntry:
    entry = MemoryEntry(id=entry_id, date=date, text=text, refs=[], layer="l2", key="chat")
    await host.store.append_entries("l2", "chat", [entry])
    return entry


def _artifact(tmp_path) -> dict:
    return json.loads(paths.semantic_path(tmp_path).read_text(encoding="utf-8"))


def _step(payload: dict) -> ScriptedLLM:
    return ScriptedLLM([ScriptedStep(chunks=[_json(payload)])])


# ---- 纯函数：窗口选择 / 校验 / 合并 ----


def test_select_recent_keeps_latest_within_budget():
    entries = [
        MemoryEntry(
            id=f"mem-{i:08x}",
            date=f"2026-09-{10 + i:02d}",
            text="文" * 100,
            refs=[],
            layer="l2",
            key="chat",
        )
        for i in range(5)
    ]
    # 每条 cost = 100 字正文 + 12 字 id + 4 = 116；260 只装得下最新两条
    picked = extract_mode.select_recent(entries, 260)
    assert [entry.id for entry in picked] == ["mem-00000003", "mem-00000004"]


def test_select_recent_keeps_one_when_budget_tiny():
    entry = MemoryEntry(id="mem-1111aaaa", date="2026-09-18", text="文" * 100, refs=[])
    # 预算小到装不下任何一条：至少保一条（否则抽取输入恒空，功能静默死掉）
    assert extract_mode.select_recent([entry], 10) == [entry]


def test_validate_chunk_fail_closed():
    allowed = {"mem-1111aaaa", "mem-2222bbbb"}
    data = {
        "entities": [
            {"name": "信号处理", "type": "topic", "refs": ["mem-1111aaaa"]},
            {"name": "用户", "type": "person", "refs": ["mem-1111aaaa"]},  # 屏蔽词
            {
                "name": "这个实体名字非常非常长已经超过二十四个字符所以必须被丢掉",
                "type": "topic",
                "refs": ["mem-1111aaaa"],
            },
            {"name": "池外引用", "type": "topic", "refs": ["mem-9999ffff"]},  # 假 ref
            {"name": "没有引用", "type": "topic", "refs": []},  # 空 refs
            {
                "name": "采样定理",
                "type": "weird",
                "refs": ["chat#mem-2222bbbb"],
            },  # 未知 type + 前缀写法
            {"name": "信号处理", "type": "topic", "refs": ["mem-2222bbbb"]},  # 同名合并
        ],
        "relations": [
            {
                "source": "信号处理",
                "target": "采样定理",
                "type": "正在学习",
                "refs": ["mem-1111aaaa"],
            },
            {"source": "信号处理", "target": "不存在", "type": "相关", "refs": ["mem-1111aaaa"]},
            {"source": "信号处理", "target": "采样定理", "type": "", "refs": ["mem-1111aaaa"]},
            {"source": "采样定理", "target": "采样定理", "type": "自环", "refs": ["mem-2222bbbb"]},
        ],
    }
    entities, relations, stats = semantic.validate_chunk(data, allowed_ids=allowed)

    assert [(e["name"], e["type"], e["refs"]) for e in entities] == [
        ("信号处理", "topic", ["mem-1111aaaa", "mem-2222bbbb"]),
        ("采样定理", "other", ["mem-2222bbbb"]),
    ]
    assert [(r["source"], r["target"], r["type"]) for r in relations] == [
        ("信号处理", "采样定理", "正在学习")
    ]
    assert stats == {"dropped_entities": 4, "dropped_relations": 3}


def test_merge_partials_merges_across_chunks():
    first = [{"id": "信号处理", "name": "信号处理", "type": "topic", "refs": ["mem-1111aaaa"]}]
    second = [
        {"id": "信号处理", "name": "信号处理", "type": "other", "refs": ["mem-2222bbbb"]},
        {"id": "采样定理", "name": "采样定理", "type": "topic", "refs": ["mem-3333cccc"]},
    ]
    edges_first = [
        {"source": "信号处理", "target": "采样定理", "type": "学习", "refs": ["mem-1111aaaa"]}
    ]
    edges_second = [
        {"source": "信号处理", "target": "采样定理", "type": "学习", "refs": ["mem-2222bbbb"]},
        {"source": "信号处理", "target": "悬空实体", "type": "相关", "refs": ["mem-2222bbbb"]},
    ]
    nodes, edges = semantic.merge_partials(
        [first, second],
        [edges_first, edges_second],
        {"mem-1111aaaa": "在做信号处理方向的学习" + "。" * 90},
    )

    merged = {node["name"]: node for node in nodes}
    assert set(merged) == {"信号处理", "采样定理"}
    # 跨块同名合并：refs 并集、type 从 other 提升、count 与样例跟上
    assert merged["信号处理"]["refs"] == ["mem-1111aaaa", "mem-2222bbbb"]
    assert merged["信号处理"]["type"] == "topic"
    assert merged["信号处理"]["count"] == 2
    assert len(merged["信号处理"]["samples"][0]) == semantic.SAMPLE_CHARS
    # 同 (source,target,type) 关系去重；端点被实体封顶挤掉的关系不保留
    assert [(e["source"], e["target"], e["type"], e["count"]) for e in edges] == [
        ("信号处理", "采样定理", "学习", 2)
    ]


def test_load_semantic_rejects_bad_artifact(tmp_path):
    path = paths.semantic_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{坏 JSON", encoding="utf-8")
    assert semantic.load_semantic(tmp_path) is None
    path.write_text(_json({"version": semantic.SEMANTIC_VERSION + 1}), encoding="utf-8")
    assert semantic.load_semantic(tmp_path) is None


# ---- extract 运行：短路顺序 ----


async def test_extract_budget_zero_short_circuits(tmp_path):
    host = _make(tmp_path, config=MemoryConfig(budget_extract=0))
    await _seed_l2(host, "用户在做信号处理方向的学习", "mem-1111aaaa")
    scripted = ScriptedLLM([])  # 任何调用都会抛「脚本耗尽」
    install_scripted(lambda: scripted)

    run = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert run.status == "ok"
    assert scripted.calls == []
    assert not paths.semantic_path(tmp_path).exists()


async def test_extract_empty_memory_makes_no_call(tmp_path):
    host = _make(tmp_path)
    scripted = ScriptedLLM([])
    install_scripted(lambda: scripted)

    run = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert run.status == "ok"
    assert scripted.calls == []
    assert not paths.semantic_path(tmp_path).exists()


async def test_extract_skips_when_source_unchanged(tmp_path):
    host = _make(tmp_path)
    await _seed_l2(host, "用户在做信号处理方向的学习", "mem-1111aaaa")
    payload = {
        "entities": [{"name": "信号处理", "type": "topic", "refs": ["mem-1111aaaa"]}],
        "relations": [],
    }
    install_scripted(lambda: _step(payload))
    first = await host.run_consolidation(trigger="manual", modes=["extract"])
    assert first.status == "ok"
    written = _artifact(tmp_path)

    second_scripted = ScriptedLLM([])
    install_scripted(lambda: second_scripted)
    second = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert second.status == "ok"
    assert second_scripted.calls == []  # 源未变：一个调用都不许发
    assert any("源未变化" in event for event in second.events)
    assert _artifact(tmp_path)["updated_at"] == written["updated_at"]


# ---- extract 运行：落盘与重抽 ----


async def test_extract_writes_artifact_with_source_and_stats(tmp_path):
    host = _make(tmp_path)
    await _seed_l2(host, "用户在做信号处理方向的学习", "mem-1111aaaa")
    await _seed_l2(host, "常看采样定理的推导", "mem-2222bbbb")
    payload = {
        "entities": [
            {"name": "信号处理", "type": "topic", "refs": ["mem-1111aaaa"]},
            {"name": "采样定理", "type": "topic", "refs": ["mem-2222bbbb"]},
        ],
        "relations": [
            {
                "source": "信号处理",
                "target": "采样定理",
                "type": "正在学习",
                "refs": ["mem-2222bbbb"],
            }
        ],
    }
    scripted = _step(payload)
    install_scripted(lambda: scripted)

    run = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert run.status == "ok"
    assert len(scripted.calls) == 1
    assert scripted.calls[0].max_tokens == extract_mode.EXTRACT_MAX_TOKENS
    request_text = scripted.calls[0].messages[-1]["content"]
    assert "mem-1111aaaa" in request_text and "用户在做信号处理方向的学习" in request_text

    artifact = _artifact(tmp_path)
    assert artifact["version"] == semantic.SEMANTIC_VERSION
    assert artifact["lang"] == "zh"
    assert artifact["source"] == {
        "mem-1111aaaa": text_digest("用户在做信号处理方向的学习"),
        "mem-2222bbbb": text_digest("常看采样定理的推导"),
    }
    assert {node["name"] for node in artifact["nodes"]} == {"信号处理", "采样定理"}
    assert [(e["source"], e["target"], e["type"]) for e in artifact["edges"]] == [
        ("信号处理", "采样定理", "正在学习")
    ]
    assert artifact["stats"] == {
        "entities": 2,
        "relations": 1,
        "dropped_entities": 0,
        "dropped_relations": 0,
        "llm_calls": 1,
    }
    assert run.stats.get("semantic_entities") == 2
    assert run.stats.get("semantic_relations") == 1


async def test_extract_bad_reply_writes_nothing(tmp_path):
    host = _make(tmp_path)
    await _seed_l2(host, "用户在做信号处理方向的学习", "mem-1111aaaa")
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["这不是 JSON"])]))

    run = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert run.status == "ok"
    assert not paths.semantic_path(tmp_path).exists()  # 宁可整份重来，不落半份
    assert any("未吃下" in event for event in run.events)


async def test_extract_reruns_after_entry_edit(tmp_path):
    host = _make(tmp_path)
    await _seed_l2(host, "用户在学信号处理", "mem-1111aaaa")
    first_payload = {
        "entities": [{"name": "信号处理", "type": "topic", "refs": ["mem-1111aaaa"]}],
        "relations": [],
    }
    install_scripted(lambda: _step(first_payload))
    await host.run_consolidation(trigger="manual", modes=["extract"])
    before = _artifact(tmp_path)

    await host.store.update_entry("l2", "chat", "mem-1111aaaa", text="用户在学数字信号处理")

    second_scripted = _step(
        {
            "entities": [{"name": "数字信号处理", "type": "topic", "refs": ["mem-1111aaaa"]}],
            "relations": [],
        }
    )
    install_scripted(lambda: second_scripted)
    run = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert run.status == "ok"
    assert len(second_scripted.calls) == 1  # 正文改过 → 重抽
    after = _artifact(tmp_path)
    assert after["updated_at"] >= before["updated_at"]
    assert [node["name"] for node in after["nodes"]] == ["数字信号处理"]
    assert after["source"]["mem-1111aaaa"] == text_digest("用户在学数字信号处理")


async def test_extract_two_chunks_merge_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mode, "EXTRACT_CHUNK_CHARS", 120)
    host = _make(tmp_path, config=MemoryConfig(budget_extract=10))
    await _seed_l2(host, "信号" * 30, "mem-1111aaaa", date="2026-09-10")
    await _seed_l2(host, "采样" * 30, "mem-2222bbbb", date="2026-09-11")

    # 单元正文 = "[日期] " 12 字 + 60 字 = 72：两条装不进一块 → 恰好两块；
    # 步序即调用序：第一块只带 mem-1111aaaa 的引用池，第二块只带 mem-2222bbbb 的
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "entities": [
                                {"name": "信号处理", "type": "topic", "refs": ["mem-1111aaaa"]}
                            ]
                        }
                    )
                ]
            ),
            ScriptedStep(
                chunks=[
                    _json(
                        {
                            "entities": [
                                {"name": "信号处理", "type": "other", "refs": ["mem-2222bbbb"]},
                                {"name": "采样定理", "type": "topic", "refs": ["mem-2222bbbb"]},
                            ],
                            "relations": [
                                {
                                    "source": "信号处理",
                                    "target": "采样定理",
                                    "type": "正在学习",
                                    "refs": ["mem-2222bbbb"],
                                }
                            ],
                        }
                    )
                ]
            ),
        ]
    )
    install_scripted(lambda: scripted)

    run = await host.run_consolidation(trigger="manual", modes=["extract"])

    assert run.status == "ok"
    assert len(scripted.calls) == 2
    artifact = _artifact(tmp_path)
    assert artifact["stats"]["llm_calls"] == 2
    merged = {node["name"]: node for node in artifact["nodes"]}
    assert merged["信号处理"]["refs"] == ["mem-1111aaaa", "mem-2222bbbb"]
    assert merged["信号处理"]["type"] == "topic"
    assert [(e["source"], e["target"]) for e in artifact["edges"]] == [("信号处理", "采样定理")]


# ---- graph_view：空态与 stale ----


def test_graph_view_empty_state(tmp_path):
    view = semantic.graph_view(tmp_path, MemoryStore(tmp_path))
    assert view == {
        "mode": "semantic",
        "updated_at": 0,
        "stale": False,
        "nodes": [],
        "edges": [],
        "stats": {},
    }


async def test_graph_view_stale_tracks_source(tmp_path):
    store = MemoryStore(tmp_path)
    await store.append_entries(
        "l2",
        "chat",
        [MemoryEntry(id="mem-1111aaaa", date="2026-09-18", text="在学信号处理", refs=[])],
    )
    source = semantic.source_map(semantic.all_entries(store))
    atomic_write_json(
        paths.semantic_path(tmp_path),
        {
            "version": semantic.SEMANTIC_VERSION,
            "updated_at": 1.0,
            "lang": "zh",
            "source": source,
            "nodes": [],
            "edges": [],
            "stats": {},
        },
    )
    assert semantic.graph_view(tmp_path, store)["stale"] is False

    await store.update_entry("l2", "chat", "mem-1111aaaa", text="改过正文")

    assert semantic.graph_view(tmp_path, store)["stale"] is True
