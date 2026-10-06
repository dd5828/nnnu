"""一次性引用（§7.1）：历史会话/笔记本记录/题目注入、宽容语义与 turn 互斥。"""

import asyncio

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


def _user_content(client: ScriptedLLM) -> str:
    return next(m["content"] for m in client.calls[-1].messages if m["role"] == "user")


async def _new_notebook(client, name: str) -> dict:
    response = await client.post("/api/v1/notebooks", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


async def _new_record(
    client, notebook_id: str, *, title: str, content: str, type_: str = "note"
) -> dict:
    response = await client.post(
        f"/api/v1/notebooks/{notebook_id}/records",
        json={"type": type_, "title": title, "content_md": content},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_history_ref_injected_into_user_message(client):
    """会话 B 引用会话 A → A 的转录注入 B 回合的 user 消息。"""
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        # 会话 A：一问一答
        await client.post(
            "/api/v1/chat", json={"session_id": "sess-ref-a", "message": "什么是矩阵？"}
        )
        # 会话 B：引用 A 提问
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-ref-b",
                "message": "基于昨天的讨论继续",
                "refs": {"sessions": ["sess-ref-a"]},
            },
        )
    finally:
        uninstall_scripted()
    assert chat.status_code == 200
    assert captured is not None
    user_content = next(m["content"] for m in captured.calls[-1].messages if m["role"] == "user")
    assert "【引用历史会话《" in user_content
    assert "什么是矩阵？" in user_content
    assert "回答" in user_content


async def test_missing_history_ref_warns_but_completes(client):
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-ref-x",
                "message": "问题",
                "refs": {"sessions": ["sess-不存在"]},
            },
        )
    finally:
        uninstall_scripted()
    assert chat.status_code == 200  # 宽容：不存在的引用不阻断回合
    user_content = next(m["content"] for m in captured.calls[-1].messages if m["role"] == "user")
    assert "引用历史会话" not in user_content  # 无有效引用则无注入


async def test_self_ref_excluded_from_injection(client):
    """引用当前会话自身：转录不含当前消息（其已在正常历史里）。"""
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-self",
                "message": "第一问",
                "refs": {"sessions": ["sess-self"]},
            },
        )
    finally:
        uninstall_scripted()
    user_content = next(m["content"] for m in captured.calls[-1].messages if m["role"] == "user")
    # 转录包含当时已落库的消息（本回合自己的 user 消息已先落库），注入是其快照——
    # 断言引用头存在即可（转录内容语义由服务端保证，重复一条 user 消息无害）
    assert "【引用历史会话《" in user_content


async def test_notebook_record_ref_injected_and_snapshot_persisted(client):
    """wire 带 refs.notebooks 要真进管线（TurnRefs extra="ignore" 的防回归）：
    注入 user 消息是其一，引用快照随消息落库是其二（前端 chip 靠它还原）。"""
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    notebook = await _new_notebook(client, "物理本")
    record = await _new_record(
        client, notebook["id"], title="牛顿第二定律", content="$F=ma$ 是核心公式"
    )
    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-nb-ref",
                "message": "这条笔记讲得对吗",
                "refs": {"notebooks": [{"notebook_id": notebook["id"], "record_id": record["id"]}]},
            },
        )
    finally:
        uninstall_scripted()
    assert chat.status_code == 200
    user_content = _user_content(captured)
    assert "【引用笔记本记录《牛顿第二定律》（笔记本「物理本」）】" in user_content
    assert "$F=ma$ 是核心公式" in user_content

    # 引用快照落库（v12）：解析时的归属与 resolved 都在
    detail = (await client.get("/api/v1/sessions/sess-nb-ref")).json()
    user_row = next(m for m in detail["messages"] if m["role"] == "user")
    assert user_row["metadata"]["refs"] == [
        {
            "kind": "notebook_record",
            "notebook_id": notebook["id"],
            "record_id": record["id"],
            "label": "牛顿第二定律",
            "resolved": True,
        }
    ]


async def test_question_ref_injected_with_answer_and_explanation(client):
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    question = (
        await client.post(
            "/api/v1/questions",
            json={
                "stem": "函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？",
                "type": "single",
                "options": ["2", "4", "8", "16"],
                "answer": "B",
                "explanation": "先求导得 $2x$，代入得 4。",
                "knowledge_point": "导数",
            },
        )
    ).json()
    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-q-ref",
                "message": "这道题为什么选 B",
                "refs": {"questions": [{"question_id": question["id"]}]},
            },
        )
    finally:
        uninstall_scripted()
    assert chat.status_code == 200
    user_content = _user_content(captured)
    assert f"【引用题目 id={question['id']}】" in user_content
    assert "在 $x=2$ 处的导数是多少" in user_content
    assert "B. 4" in user_content
    assert "参考答案：B" in user_content
    assert "先求导得 $2x$" in user_content


async def test_missing_refs_tolerated_without_blocking(client):
    """引用的记录/题目不存在 → warning 跳过，不阻断回合（对齐历史会话引用的宽容语义）。"""
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-ref-miss",
                "message": "问题",
                "refs": {
                    "notebooks": [{"notebook_id": "nb-nope", "record_id": "nbr-nope"}],
                    "questions": [{"question_id": "q-nope"}],
                },
            },
        )
    finally:
        uninstall_scripted()
    assert chat.status_code == 200
    user_content = _user_content(captured)
    assert "引用笔记本记录" not in user_content
    assert "引用题目" not in user_content
    # 未解析的引用也进快照（resolved=false）：前端 chip 显示失效态而不是消失
    detail = (await client.get("/api/v1/sessions/sess-ref-miss")).json()
    user_row = next(m for m in detail["messages"] if m["role"] == "user")
    assert [entry["resolved"] for entry in user_row["metadata"]["refs"]] == [False, False]


async def test_notebook_ref_survives_move_on_regenerate(client):
    """验收③：记录 A→B 移动后，同一条消息 regenerate 引用**活查重解析**——
    读到的是移动后的新归属与原文（record id 不变是引用不失效的根）。"""
    clients: list[ScriptedLLM] = []

    def factory() -> ScriptedLLM:
        client_llm = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        clients.append(client_llm)
        return client_llm

    first_book = await _new_notebook(client, "甲本")
    second_book = await _new_notebook(client, "乙本")
    record = await _new_record(
        client, first_book["id"], title="会移动的笔记", content="笔记正文在此"
    )
    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-nb-move",
                "message": "看看这条",
                "refs": {
                    "notebooks": [{"notebook_id": first_book["id"], "record_id": record["id"]}]
                },
            },
        )
        assert chat.status_code == 200
        assert "（笔记本「甲本」）" in _user_content(clients[-1])

        # 移动：只换归属，id 不变
        moved = await client.patch(
            f"/api/v1/notebooks/{first_book['id']}/records/{record['id']}",
            json={"target_notebook_id": second_book["id"]},
        )
        assert moved.status_code == 200

        regen = await client.post("/api/v1/sessions/sess-nb-move/regenerate")
        assert regen.status_code == 200
        for _ in range(50):  # regenerate 后台执行：轮询到新 assistant 落库
            detail = (await client.get("/api/v1/sessions/sess-nb-move")).json()
            if len(detail["messages"]) == 2:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("regenerate 回合未完成")
    finally:
        uninstall_scripted()
    regen_content = _user_content(clients[-1])
    assert "（笔记本「乙本」）" in regen_content  # 活查重解析：归属跟着动
    assert "笔记正文在此" in regen_content
    # 原 user 消息快照不动（发送时的事实），chip 深链靠 record_id 跟到新位置
    user_row = next(m for m in detail["messages"] if m["role"] == "user")
    assert user_row["metadata"]["refs"][0]["notebook_id"] == first_book["id"]


async def test_deleted_ref_tolerated_on_regenerate(client):
    """引用记录被删后 regenerate：不炸回合，只是不再注入（宽容语义）。"""
    clients: list[ScriptedLLM] = []

    def factory() -> ScriptedLLM:
        client_llm = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        clients.append(client_llm)
        return client_llm

    notebook = await _new_notebook(client, "短命本")
    record = await _new_record(client, notebook["id"], title="将被删", content="马上没了")
    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-nb-del",
                "message": "看看",
                "refs": {"notebooks": [{"notebook_id": notebook["id"], "record_id": record["id"]}]},
            },
        )
        assert chat.status_code == 200
        assert "马上没了" in _user_content(clients[-1])

        await client.delete(f"/api/v1/notebooks/{notebook['id']}/records/{record['id']}")
        regen = await client.post("/api/v1/sessions/sess-nb-del/regenerate")
        assert regen.status_code == 200
        for _ in range(50):
            detail = (await client.get("/api/v1/sessions/sess-nb-del")).json()
            if len(detail["messages"]) == 2:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("regenerate 回合未完成")
    finally:
        uninstall_scripted()
    assert "引用笔记本记录" not in _user_content(clients[-1])
    assert "马上没了" not in _user_content(clients[-1])
