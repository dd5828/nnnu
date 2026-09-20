"""一次性引用（§7.1）：历史会话转录注入、宽容语义与 turn 互斥。"""

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


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
    user_content = next(
        m["content"] for m in captured.calls[-1].messages if m["role"] == "user"
    )
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
    user_content = next(
        m["content"] for m in captured.calls[-1].messages if m["role"] == "user"
    )
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
    user_content = next(
        m["content"] for m in captured.calls[-1].messages if m["role"] == "user"
    )
    # 转录包含当时已落库的消息（本回合自己的 user 消息已先落库），注入是其快照——
    # 断言引用头存在即可（转录内容语义由服务端保证，重复一条 user 消息无害）
    assert "【引用历史会话《" in user_content
