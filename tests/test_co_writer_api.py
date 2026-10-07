"""Co-Writer REST（§7.13）：文档 CRUD、改写发起/确认/拒绝、错误信封、记账与不落库。

app_client 同 conftest.client（跑 lifespan）但把 app 也交出来：编辑端点要断言
「伪上下文不入库」与 usage_records 的合成 turn_id，得拿到 app.state.db。
编辑路径渲染提示词，所以整组用例都挂着 repo_prompts。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

pytestmark = pytest.mark.usefixtures("repo_prompts")

DOC = "# 学习笔记\n\n北京是中国的首都。\n"
SELECTION = "北京是中国的首都。"


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture
async def app_client(tmp_home):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c


async def _new_doc(client, *, content: str = DOC, title: str = "") -> dict:
    response = await client.post("/api/v1/co-writer", json={"title": title, "content": content})
    assert response.status_code == 200, response.text
    return response.json()


async def _ok_edit(client, doc_id: str, **overrides):
    start = DOC.index(SELECTION)
    body = {
        "start": start,
        "end": start + len(SELECTION),
        "original": SELECTION,
        "action": "expand",
        "instruction": "写得更详细",
        **overrides,
    }
    return await client.post(f"/api/v1/co-writer/{doc_id}/edit", json=body)


async def _counts(app) -> dict[str, int]:
    result = {}
    for table in ("sessions", "messages", "co_writer_docs"):
        row = await app.state.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
        result[table] = int(row["n"])
    return result


async def test_doc_crud_round_trip(app_client, tmp_home):
    app, client = app_client
    doc = await _new_doc(client)
    assert doc["id"].startswith("cw-")
    assert doc["title"] == "学习笔记"  # 没给标题 → 正文首行（剥 markdown 标题符号）

    listed = (await client.get("/api/v1/co-writer")).json()["docs"]
    assert [item["id"] for item in listed] == [doc["id"]]
    assert listed[0]["preview"] == "学习笔记"

    detail = (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()
    assert detail["content"] == DOC

    path = tmp_home / "data" / "user" / "co_writer" / f"{doc['id']}.md"
    assert path.read_text(encoding="utf-8") == DOC

    renamed = (
        await client.patch(f"/api/v1/co-writer/{doc['id']}", json={"title": "  新标题 "})
    ).json()
    assert renamed["title"] == "新标题"

    # 自动保存只改正文：标题不动（PATCH content 不重新取题）
    saved = (
        await client.patch(f"/api/v1/co-writer/{doc['id']}", json={"content": "第一行\n第二行"})
    ).json()
    assert saved["title"] == "新标题"
    assert saved["updated_at"] >= doc["updated_at"]
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()[
        "content"
    ] == "第一行\n第二行"

    # 两个字段都没给 = 空体 422
    empty = await client.patch(f"/api/v1/co-writer/{doc['id']}", json={})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_request"

    assert (await client.delete(f"/api/v1/co-writer/{doc['id']}")).json() == {"deleted": doc["id"]}
    assert not path.exists()
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).status_code == 404
    assert (await client.delete(f"/api/v1/co-writer/{doc['id']}")).status_code == 404
    assert (await client.get("/api/v1/co-writer")).json()["docs"] == []


async def test_missing_doc_and_bad_id_404(app_client):
    _, client = app_client
    assert (await client.get("/api/v1/co-writer/cw-00000000")).status_code == 404
    # 形状不对的 id 一律当不存在（防路径穿越的同一道闸）
    assert (await client.get("/api/v1/co-writer/not-a-doc-id")).status_code == 404
    assert (await client.delete("/api/v1/co-writer/nb-abcd1234")).status_code == 404


async def test_oversize_content_422(app_client):
    _, client = app_client
    created = await client.post("/api/v1/co-writer", json={"content": "字" * 200_001})
    assert created.status_code == 422
    assert created.json()["error"]["code"] == "invalid_content"
    doc = await _new_doc(client)
    patched = await client.patch(f"/api/v1/co-writer/{doc['id']}", json={"content": "字" * 200_001})
    assert patched.status_code == 422
    assert patched.json()["error"]["code"] == "invalid_content"
    # 校验失败不动原文件
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()["content"] == DOC


async def test_edit_accept_replaces_selection(app_client):
    app, client = app_client
    doc = await _new_doc(client)
    before = await _counts(app)
    install_scripted(
        lambda: ScriptedLLM(
            [
                ScriptedStep(
                    chunks=["北京是中国的首都，", "也是政治与文化中心。"],
                    usage={"prompt_tokens": 100, "completion_tokens": 20},
                )
            ]
        )
    )
    response = await _ok_edit(client, doc["id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["edit_id"].startswith("cwe-")
    assert result["original"] == SELECTION
    assert result["edited"] == "北京是中国的首都，也是政治与文化中心。"
    assert result["stats"] == {"added": 1, "deleted": 1, "kept": 0}
    assert result["trace"] == [] and result["citations"] == []
    assert result["degraded"] is None and result["model"]
    # 确认前文档不变（待确认编辑只在内存里挂着）
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()["content"] == DOC
    # 伪上下文不入库：三个表行数原样
    assert await _counts(app) == before
    # 记账：合成 turn_id cowrite-<edit_id> 写进 usage_records
    row = await app.state.db.fetch_one(
        "SELECT turn_id, input_tokens, output_tokens FROM usage_records WHERE turn_id = ?",
        (f"cowrite-{result['edit_id']}",),
    )
    assert row is not None and row["input_tokens"] == 100 and row["output_tokens"] == 20

    start = DOC.index(SELECTION)
    expected = DOC[:start] + result["edited"] + DOC[start + len(SELECTION) :]
    accepted = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/{result['edit_id']}", json={"action": "accept"}
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["applied"] is True and body["doc"]["id"] == doc["id"]
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()["content"] == expected
    # 一条编辑只能消费一次：再 accept 就是过期
    again = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/{result['edit_id']}", json={"action": "accept"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "edit_expired"
    assert again.json()["error"]["recoverable"] is True
    # 写回也没动会话/消息
    assert (await _counts(app))["sessions"] == before["sessions"]
    assert (await _counts(app))["messages"] == before["messages"]


async def test_edit_reject_keeps_doc(app_client):
    _, client = app_client
    doc = await _new_doc(client)
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["改写后的文字。"])]))
    result = (await _ok_edit(client, doc["id"])).json()
    rejected = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/{result['edit_id']}", json={"action": "reject"}
    )
    assert rejected.status_code == 200
    assert rejected.json() == {"rejected": result["edit_id"]}
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()["content"] == DOC
    # 拒绝同样只能消费一次
    again = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/{result['edit_id']}", json={"action": "reject"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "edit_expired"


async def test_unknown_edit_and_bad_action(app_client):
    _, client = app_client
    doc = await _new_doc(client)
    missing = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/cwe-00000000", json={"action": "accept"}
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "edit_expired"
    bad = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/cwe-00000000", json={"action": "apply"}
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_action"


async def test_edit_original_mismatch_409(app_client):
    _, client = app_client
    doc = await _new_doc(client)
    response = await _ok_edit(client, doc["id"], original="上海是中国的首都。")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "doc_changed"
    assert body["error"]["recoverable"] is True


async def test_edit_accept_after_doc_edited_409(app_client):
    _, client = app_client
    doc = await _new_doc(client)
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["改写后的文字。"])]))
    result = (await _ok_edit(client, doc["id"])).json()
    # 待确认期间自动保存把正文写新了（模拟前端 2s 防抖 PATCH 先落库）
    await client.patch(f"/api/v1/co-writer/{doc['id']}", json={"content": DOC + "补充一句。"})
    response = await client.post(
        f"/api/v1/co-writer/{doc['id']}/edit/{result['edit_id']}", json={"action": "accept"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "doc_changed"
    # 拒绝不受影响
    assert (await client.get(f"/api/v1/co-writer/{doc['id']}")).json()[
        "content"
    ] == DOC + "补充一句。"


async def test_edit_validation_422(app_client):
    _, client = app_client
    doc = await _new_doc(client)
    out_of_range = await _ok_edit(client, doc["id"], start=0, end=9999)
    assert out_of_range.status_code == 422
    assert out_of_range.json()["error"]["code"] == "invalid_selection"
    unknown_action = await _ok_edit(client, doc["id"], action="summarize")
    assert unknown_action.status_code == 422
    assert unknown_action.json()["error"]["code"] == "invalid_action"
    free_without_instruction = await _ok_edit(client, doc["id"], action="free", instruction="   ")
    assert free_without_instruction.status_code == 422
    assert free_without_instruction.json()["error"]["code"] == "invalid_action"


async def test_edit_missing_doc_404(app_client):
    _, client = app_client
    assert (await _ok_edit(client, "cw-00000000")).status_code == 404
    assert (await _ok_edit(client, "nope")).status_code == 404


async def test_edit_output_invalid_502(app_client):
    _, client = app_client
    doc = await _new_doc(client)
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=[])]))
    response = await _ok_edit(client, doc["id"])
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "llm_output_invalid"
    assert body["error"]["recoverable"] is True


async def test_edit_llm_unavailable_503(app_client, monkeypatch):
    _, client = app_client
    doc = await _new_doc(client)
    # 不能靠「临时 home 没配模型」：本机环境变量里可能就有真配置（会真打网络）。
    # 直接把取配置这步钉成抛错，测路由的 503 映射。
    import nnnu.co_writer.edit as edit_module
    from nnnu.services.llm.errors import LLMConfigError

    def _no_model():
        raise LLMConfigError("没有配置模型")

    monkeypatch.setattr(edit_module, "resolve_model_config", _no_model)
    response = await _ok_edit(client, doc["id"])
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "llm_unavailable"
    assert body["error"]["recoverable"] is True
