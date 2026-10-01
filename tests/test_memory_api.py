"""记忆 REST（§7.10）：三层浏览、证据链图谱、整合触发、条目编辑与只读边界。

零 LLM 通路：整合用例走「空状态空跑」或替身（预算 0 / 无输入 → 不调模型）。
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

import nnnu.services.memory.service as memory_service_module
from nnnu.core.ids import new_id
from nnnu.core.stream_bus import StreamBus
from nnnu.services.llm.factory import uninstall_scripted
from nnnu.services.memory.models import MemoryEntry, text_digest
from nnnu.services.memory.state import doc_key


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


async def _seed_turn(app, *, turn_id: str = "turn-1", text: str = "帮我把傅里叶变换讲清楚"):
    """走真实 L1 接缝落三行（user_message / assistant_done / cost），返回引用锚点。"""
    service = app.state.memory
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
    ref = service.turn_ref(turn_id)
    assert ref is not None
    return ref


def _seed_entry(app, layer: str, key: str, *, text: str, refs: list[str]) -> str:
    """直接落一条带 id 的条目并登记账本（等价于 consolidator 写过一轮）。"""
    service = app.state.memory
    entry = MemoryEntry(
        id=new_id("mem"), date="2026-09-18", text=text, refs=refs, layer=layer, key=key
    )
    doc = service.store.load(layer, key)
    doc.entries.append(entry)
    service.store.save(layer, key, doc)
    service.state.register_entry(doc_key(layer, key), entry.id, text_digest(text), "consolidator")
    return entry.id


def _l1_ref(position) -> str:
    surface, filename, line = position
    return f"L1:{surface}/{filename}#{line}"


# ---- 概览与错误信封 ----


async def test_overview_empty_state(hub):
    client, _app = hub
    resp = await client.get("/api/v1/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["surfaces"]) == 7 and body["surfaces"][0] == "chat"
    assert body["l3_docs"] == ["profile", "recent", "scope", "preferences"]
    assert body["l1"]["chat"] == {"files": [], "lines": 0, "bytes": 0, "events": {}}
    assert body["l2"]["chat"]["entries"] == 0
    assert body["turns_since_consolidation"] == 0
    assert body["last_run"] is None and body["consolidating"] is False
    # 设置区默认值直出（规格默认，§7.10）
    assert body["config"]["auto_threshold_turns"] == 20
    assert body["config"]["budget_update"] == 3 and body["config"]["trace_enabled"] is True


async def test_overview_counts_l1_files(hub):
    """概览的 L1 统计要按真实文件数（回归：曾把带后缀的文件名喂给 trace_path，拼成 .jsonl.jsonl 恒 0）。"""
    client, app = hub
    await _seed_turn(app)
    resp = await client.get("/api/v1/memory")
    assert resp.status_code == 200
    l1 = resp.json()["l1"]["chat"]
    assert len(l1["files"]) == 1 and l1["files"][0].endswith(".jsonl")
    assert l1["lines"] == 3
    assert l1["bytes"] > 0
    assert l1["events"] == {"user_message": 1, "assistant_done": 1, "cost": 1}


async def test_error_envelope_shape(hub):
    client, _app = hub
    resp = await client.get("/api/v1/memory/l1", params={"surface": "nope"})
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert set(error) == {"code", "message", "recoverable"}
    assert error["code"] == "not_found" and error["recoverable"] is False


async def test_memory_disabled_503(hub):
    client, app = hub
    app.state.memory = None
    for method, url, kwargs in (
        ("get", "/api/v1/memory", {}),
        ("get", "/api/v1/memory/graph", {}),
        ("post", "/api/v1/memory/consolidate", {"json": {}}),
        ("patch", "/api/v1/memory/l2/mem-00000000", {"json": {"text": "x"}}),
        ("delete", "/api/v1/memory/l3/mem-00000000", {}),
    ):
        resp = await getattr(client, method)(url, **kwargs)
        assert resp.status_code == 503, url
        assert resp.json()["error"]["code"] == "memory_disabled"


# ---- L1 只读浏览 ----


async def test_l1_page_and_pagination(hub):
    client, app = hub
    await _seed_turn(app)
    resp = await client.get("/api/v1/memory/l1", params={"surface": "chat"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3 and len(body["rows"]) == 3
    assert [row["event"] for row in body["rows"]] == ["user_message", "assistant_done", "cost"]
    assert body["rows"][0]["data"]["seq"] == 1
    assert body["file"] == body["files"][-1] and body["file"].endswith(".jsonl")

    page = (
        await client.get("/api/v1/memory/l1", params={"surface": "chat", "offset": 1, "limit": 1})
    ).json()
    assert page["total"] == 3 and [row["data"]["seq"] for row in page["rows"]] == [2]


async def test_l1_unknown_surface_and_file(hub):
    client, _app = hub
    assert (await client.get("/api/v1/memory/l1", params={"surface": "nope"})).status_code == 404
    resp = await client.get(
        "/api/v1/memory/l1", params={"surface": "chat", "file": "1999-01.jsonl"}
    )
    assert resp.status_code == 404


async def test_l1_is_read_only(hub):
    client, _app = hub
    patch = await client.patch("/api/v1/memory/l1/mem-00000000", json={"text": "改不动"})
    delete = await client.delete("/api/v1/memory/l1/mem-00000000")
    assert patch.status_code == 409 and patch.json()["error"]["code"] == "read_only_layer"
    assert delete.status_code == 409 and delete.json()["error"]["code"] == "read_only_layer"


# ---- L2 / L3 浏览与编辑 ----


async def test_l2_l3_docs_and_edited_flags(hub):
    client, app = hub
    position = await _seed_turn(app)
    entry_id = _seed_entry(
        app, "l2", "chat", text="用户在做信号处理方向的学习", refs=[_l1_ref(position)]
    )

    body = (await client.get("/api/v1/memory/l2", params={"surface": "chat"})).json()
    assert len(body["docs"]) == 1
    doc = body["docs"][0]
    assert doc["key"] == "chat" and doc["stats"] == {
        "entries": 1,
        "stale": 0,
        "edited": 0,
        "anonymous": 0,
    }
    assert doc["entries"][0]["id"] == entry_id
    assert doc["entries"][0]["refs"] == [_l1_ref(position)]
    assert doc["entries"][0]["origin"] == "consolidator"
    assert entry_id in doc["text"]  # 原文里带 id 方括号

    # L3 三篇空 + 选一篇给条目：doc 过滤
    all_l3 = (await client.get("/api/v1/memory/l3")).json()
    assert [item["key"] for item in all_l3["docs"]] == ["profile", "recent", "scope", "preferences"]
    l3_id = _seed_entry(
        app, "l3", "profile", text="学习动机以工程落地为主", refs=[f"L2:chat#{entry_id}"]
    )
    one = (await client.get("/api/v1/memory/l3", params={"doc": "profile"})).json()
    assert one["docs"][0]["entries"][0]["id"] == l3_id
    assert (await client.get("/api/v1/memory/l3", params={"doc": "nope"})).status_code == 404


async def test_patch_entry_marks_edited_then_handback(hub):
    client, app = hub
    entry_id = _seed_entry(app, "l2", "chat", text="原措辞", refs=[])

    resp = await client.patch(f"/api/v1/memory/l2/{entry_id}", json={"text": "  人工改过的措辞  "})
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    assert entry["text"] == "人工改过的措辞"  # 落盘前 strip
    assert entry["edited"] is True and entry["origin"] == "human"
    # 文件真改了、id 保留、账本哈希同步
    stored = app.state.memory.store.find_in("l2", "chat", entry_id)
    assert stored is not None and stored.text == "人工改过的措辞"
    meta = app.state.memory.state.entry_meta(doc_key("l2", "chat"), entry_id)
    assert meta is not None and meta["edited"] is True
    assert meta["hash"] == text_digest("人工改过的措辞")

    back = await client.patch(f"/api/v1/memory/l2/{entry_id}", json={"managed": True})
    assert back.status_code == 200
    assert back.json()["entry"]["edited"] is False
    assert back.json()["entry"]["origin"] == "consolidator"


async def test_patch_entry_validation(hub):
    client, app = hub
    entry_id = _seed_entry(app, "l2", "chat", text="原措辞", refs=[])
    assert (await client.patch(f"/api/v1/memory/l2/{entry_id}", json={})).status_code == 422
    empty = await client.patch(f"/api/v1/memory/l2/{entry_id}", json={"text": "   "})
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "invalid_entry"
    long = await client.patch(f"/api/v1/memory/l2/{entry_id}", json={"text": "字" * 401})
    assert long.status_code == 422
    assert (
        await client.patch("/api/v1/memory/l4/mem-00000000", json={"text": "x"})
    ).status_code == 404
    assert (
        await client.patch("/api/v1/memory/l2/mem-00000000", json={"text": "x"})
    ).status_code == 404


async def test_delete_entry_tombstone_and_meta_drop(hub):
    client, app = hub
    entry_id = _seed_entry(app, "l2", "chat", text="要删的条目", refs=[])
    resp = await client.delete(f"/api/v1/memory/l2/{entry_id}")
    assert resp.status_code == 200 and resp.json()["deleted"] == entry_id
    assert app.state.memory.store.find_in("l2", "chat", entry_id) is None
    assert entry_id in app.state.memory.state.tombstone_ids()  # 防 LLM 下轮写回来
    assert app.state.memory.state.entry_meta(doc_key("l2", "chat"), entry_id) is None
    assert (await client.delete(f"/api/v1/memory/l2/{entry_id}")).status_code == 404


# ---- 证据链图谱（验收②）----


async def _seed_chain(app):
    """L1 行 ← L2 条目 ← L3 条目，全链可解析；返回 (l2_id, l3_id, l1_body)。"""
    position = await _seed_turn(app)
    l1_body = _l1_ref(position)[len("L1:") :]
    l2_id = _seed_entry(
        app, "l2", "chat", text="用户在做信号处理方向的学习", refs=[f"L1:{l1_body}"]
    )
    l3_id = _seed_entry(
        app, "l3", "profile", text="工程落地取向的学习者", refs=[f"L2:chat#{l2_id}"]
    )
    return l2_id, l3_id, l1_body


async def test_graph_overview_links_l3_to_l2(hub):
    client, app = hub
    l2_id, l3_id, _ = await _seed_chain(app)
    body = (await client.get("/api/v1/memory/graph")).json()
    assert body["root"] is None
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id[l3_id]["layer"] == "l3" and by_id[l2_id]["layer"] == "l2"
    assert {"source": l3_id, "target": l2_id} in body["edges"]
    assert all(node["layer"] != "l1" for node in body["nodes"])  # 全景不自动拉 L1


async def test_graph_depth_two_reaches_l1_rows(hub):
    client, app = hub
    l2_id, l3_id, l1_body = await _seed_chain(app)
    depth1 = (await client.get("/api/v1/memory/graph", params={"entry": l3_id, "depth": 1})).json()
    assert depth1["root"] == l3_id
    assert {node["id"] for node in depth1["nodes"]} == {l3_id, l2_id}

    depth2 = (await client.get("/api/v1/memory/graph", params={"entry": l3_id, "depth": 2})).json()
    by_id = {node["id"]: node for node in depth2["nodes"]}
    assert by_id[l1_body]["layer"] == "l1" and by_id[l1_body]["broken"] is False
    assert by_id[l1_body]["text"] == "帮我把傅里叶变换讲清楚"
    assert by_id[l1_body]["kind"] == "user_message"
    assert {"source": l2_id, "target": l1_body} in depth2["edges"]


async def test_graph_marks_broken_refs(hub):
    client, app = hub
    # L3 引用一个不存在的 L2 条目；L2 引用一个不存在的 L1 行
    orphan_l3 = _seed_entry(
        app, "l3", "profile", text="引用已删条目", refs=["L2:chat#mem-deadbeef"]
    )
    dangling_l2 = _seed_entry(
        app, "l2", "chat", text="引用不存在的事件行", refs=["L1:chat/2026-09.jsonl#999"]
    )

    broken_l2 = (
        await client.get("/api/v1/memory/graph", params={"entry": orphan_l3, "depth": 1})
    ).json()
    node = next(item for item in broken_l2["nodes"] if item["id"] == "L2:chat#mem-deadbeef")
    assert node["broken"] is True and node["layer"] == "l2"

    broken_l1 = (
        await client.get("/api/v1/memory/graph", params={"entry": dangling_l2, "depth": 1})
    ).json()
    line = next(item for item in broken_l1["nodes"] if item["layer"] == "l1")
    assert line["broken"] is True and line["id"] == "chat/2026-09.jsonl#999"


async def test_graph_validation(hub):
    client, _app = hub
    assert (await client.get("/api/v1/memory/graph", params={"depth": 3})).status_code == 422
    bad = await client.get("/api/v1/memory/graph", params={"entry": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_entry"
    assert (
        await client.get("/api/v1/memory/graph", params={"entry": "mem-00000000"})
    ).status_code == 404


# ---- 整合触发（202 / 409 单飞）----


async def test_consolidate_202_then_run_recorded(hub):
    client, app = hub
    resp = await client.post("/api/v1/memory/consolidate", json={})
    assert resp.status_code == 202
    run = resp.json()["run"]
    assert (
        run["status"] == "queued" and run["trigger"] == "manual" and run["id"].startswith("mrun-")
    )

    deadline = 200
    last = None
    while deadline > 0:
        last = app.state.memory.state.last_run()
        if last is not None and last["status"] != "running":
            break
        await asyncio.sleep(0.01)
        deadline -= 1
    assert last is not None and last["status"] == "ok"  # 空状态空跑，零 LLM
    overview = (await client.get("/api/v1/memory")).json()
    assert overview["consolidating"] is False
    assert overview["last_run"]["id"] == run["id"]  # 202 回的 id 就是落账那个


async def test_consolidate_busy_409(hub, monkeypatch):
    client, _app = hub
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GateConsolidator:
        def __init__(self, **kwargs) -> None:
            pass

        async def run_consolidation(self, run=None, **kwargs):
            entered.set()
            await release.wait()
            if run is not None:
                run.status = "ok"
            return run

    monkeypatch.setattr(memory_service_module, "Consolidator", _GateConsolidator)
    first = await client.post("/api/v1/memory/consolidate", json={})
    assert first.status_code == 202
    await asyncio.wait_for(entered.wait(), 1.0)
    second = await client.post("/api/v1/memory/consolidate", json={})
    assert second.status_code == 409 and second.json()["error"]["code"] == "busy"
    release.set()
    for _ in range(50):
        if not _app.state.memory.busy:
            break
        await asyncio.sleep(0.01)
    assert (await client.post("/api/v1/memory/consolidate", json={})).status_code == 202


async def test_consolidate_validation(hub):
    client, _app = hub
    bad_mode = await client.post("/api/v1/memory/consolidate", json={"modes": ["nope"]})
    assert bad_mode.status_code == 422 and bad_mode.json()["error"]["code"] == "invalid_entry"
    bad_surface = await client.post("/api/v1/memory/consolidate", json={"surfaces": ["nope"]})
    assert bad_surface.status_code == 422


# ---- 设置区 ----


async def test_settings_area_memory(hub):
    client, _app = hub
    body = (await client.get("/api/v1/settings/memory")).json()
    assert body["values"]["auto_threshold_turns"] == 20
    assert body["values"]["update_chunk_chars"] == 3000
    keys = {field["key"] for field in body["fields"]}
    assert keys == {
        "auto_enabled",
        "auto_threshold_turns",
        "inject_enabled",
        "budget_update",
        "budget_audit",
        "budget_dedup",
        "budget_extract",
        "update_chunk_chars",
        "trace_enabled",
    }
    ok = await client.put("/api/v1/settings/memory", json={"values": {"budget_update": 0}})
    assert ok.status_code == 200 and ok.json()["values"]["budget_update"] == 0
    bad = await client.put(
        "/api/v1/settings/memory", json={"values": {"auto_threshold_turns": 999}}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
