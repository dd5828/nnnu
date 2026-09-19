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
