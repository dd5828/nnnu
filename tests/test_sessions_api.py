"""会话 REST：列表/新建/详情/重命名/删除/重新生成。"""

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


async def test_list_create_rename_delete(client):
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert session_id.startswith("sess-")

    resp = await client.get("/api/v1/sessions")
    assert resp.status_code == 200
    assert any(s["id"] == session_id for s in resp.json()["sessions"])

    renamed = await client.patch(f"/api/v1/sessions/{session_id}", json={"title": "新标题"})
    assert renamed.json()["title"] == "新标题"

    detail = await client.get(f"/api/v1/sessions/{session_id}")
    assert detail.json()["messages"] == []

    deleted = await client.delete(f"/api/v1/sessions/{session_id}")
    assert deleted.status_code == 200
    assert (await client.get(f"/api/v1/sessions/{session_id}")).status_code == 404


async def test_detail_with_messages(client):
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["回答"])]))
    chat = await client.post("/api/v1/chat", json={"session_id": "sess-detail", "message": "问题"})
    assert chat.status_code == 200
    detail = await client.get("/api/v1/sessions/sess-detail")
    roles = [m["role"] for m in detail.json()["messages"]]
    assert roles == ["user", "assistant"]
    uninstall_scripted()


async def test_regenerate_endpoint(client):
    calls = []

    def factory():
        calls.append(1)
        return ScriptedLLM([ScriptedStep(chunks=[f"版本{len(calls)}"])])

    install_scripted(factory)
    first = await client.post("/api/v1/chat", json={"session_id": "sess-regen", "message": "问题"})
    assert first.json()["response"] == "版本1"
    regen = await client.post("/api/v1/sessions/sess-regen/regenerate")
    assert regen.status_code == 200
    # regenerate 回合后台执行：轮询直到新 assistant 落库
    import asyncio

    contents = []
    for _ in range(50):
        detail = await client.get("/api/v1/sessions/sess-regen")
        contents = [m["content"] for m in detail.json()["messages"]]
        if len(contents) == 2:
            break
        await asyncio.sleep(0.05)
    assert contents == ["问题", "版本2"]
    uninstall_scripted()


async def test_kb_selection_is_sticky_per_session(client):
    """§7.9：带 kb_ids 的回合全量替换并落库；之后的回合不带也沿用（regenerate 不误清）。"""
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["回答"])]))
    chat = await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-kb", "message": "问题", "kb_ids": ["kb-1", "kb-2"]},
    )
    assert chat.status_code == 200
    detail = await client.get("/api/v1/sessions/sess-kb")
    assert detail.json()["kb_ids"] == ["kb-1", "kb-2"]

    # regenerate 构造的 TurnRequest 不带 kb_ids：沿用会话现值，不清空
    regen = await client.post("/api/v1/sessions/sess-kb/regenerate")
    assert regen.status_code == 200

    import asyncio

    for _ in range(50):
        detail = await client.get("/api/v1/sessions/sess-kb")
        if len(detail.json()["messages"]) == 2:
            break
        await asyncio.sleep(0.05)
    assert detail.json()["kb_ids"] == ["kb-1", "kb-2"]

    # 显式空列表 = 用户点掉了全部芯片（全量替换成空，而不是"什么都不改"）
    again = await client.post(
        "/api/v1/chat", json={"session_id": "sess-kb", "message": "再问", "kb_ids": []}
    )
    assert again.status_code == 200
    detail = await client.get("/api/v1/sessions/sess-kb")
    assert detail.json()["kb_ids"] == []
    uninstall_scripted()
