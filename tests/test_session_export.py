"""会话导出（§7.1）：完整 Markdown 转录与 citations 落库。"""

import time

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.sessions.export import export_session_markdown
from nnnu.services.sessions.models import Message, Session


def test_export_pure_function_includes_all_parts():
    session = Session(id="sess-x", title="导出测试", language="zh", updated_at=1700000000)
    messages = [
        Message(
            id="m1",
            session_id="sess-x",
            role="user",
            content="问题",
            created_at=1699999900,
        ),
        Message(
            id="m2",
            session_id="sess-x",
            role="assistant",
            content="答案正文",
            thinking="先想一下",
            tool_calls=[
                {"tool_name": "attachment_search", "call_id": "c1", "ok": True, "summary": "命中 1 段"}
            ],
            citations=[{"doc_id": "att-1", "kb": "attachment", "page": 2, "snippet": "片段"}],
            cost={"tokens": 100, "cost": 0.01, "per_model": {}},
            created_at=1699999950,
        ),
    ]
    markdown = export_session_markdown(session, messages)
    assert markdown.startswith("# 导出测试")
    assert "## 用户" in markdown
    assert "## 助手" in markdown
    assert "问题" in markdown
    assert "答案正文" in markdown
    assert "> 思考：" in markdown and "先想一下" in markdown
    assert "🔧 工具：attachment_search（成功）" in markdown
    assert "📖 引用：att-1 第2页：片段" in markdown
    assert "100 tokens / $0.01" in markdown


def test_export_english_labels():
    session = Session(id="sess-en", title="Export", language="en", updated_at=time.time())
    messages = [
        Message(id="m1", session_id="sess-en", role="user", content="Hi", created_at=time.time())
    ]
    markdown = export_session_markdown(session, messages)
    assert "## User" in markdown
    assert "## 用户" not in markdown


async def test_export_endpoint_and_citations_persisted(client):
    """端到端：带引用的回合 → 导出 Markdown 含引用（citations 落库链路）。"""
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["回答内容"])]))
    try:
        await client.post("/api/v1/chat", json={"session_id": "sess-export", "message": "问题"})
    finally:
        uninstall_scripted()

    resp = await client.get("/api/v1/sessions/sess-export/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "content-disposition" in resp.headers
    body = resp.text
    assert "问题" in body and "回答内容" in body

    # 详情消息含 citations 字段（落库链路存在）
    detail = await client.get("/api/v1/sessions/sess-export")
    assistant = [m for m in detail.json()["messages"] if m["role"] == "assistant"][0]
    assert assistant["citations"] == []


async def test_export_missing_session_404(client):
    resp = await client.get("/api/v1/sessions/sess-none/export")
    assert resp.status_code == 404


async def test_citations_persisted_into_assistant_message(client):
    """引用落库：回合产生 citation 后，assistant 消息的 citations 字段持久化。"""
    import json

    import fitz

    from nnnu.services.llm.protocol import LLMToolCall

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "needle content")
    pdf_bytes = doc.tobytes()
    doc.close()

    upload = await client.post(
        "/api/v1/attachments",
        files={"file": ("t.pdf", pdf_bytes, "application/pdf")},
        data={"session_id": "sess-cit"},
    )
    attachment = upload.json()
    install_scripted(
        lambda: ScriptedLLM(
            [
                ScriptedStep(
                    tool_calls=[
                        LLMToolCall(
                            id="c1",
                            name="attachment_search",
                            arguments=json.dumps({"query": "needle"}),
                        )
                    ]
                ),
                ScriptedStep(chunks=["回答"]),
            ]
        )
    )
    try:
        await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-cit",
                "message": "问题",
                "attachments": [{"id": attachment["id"], "name": "t.pdf"}],
            },
        )
    finally:
        uninstall_scripted()
    detail = await client.get("/api/v1/sessions/sess-cit")
    assistant = [m for m in detail.json()["messages"] if m["role"] == "assistant"][0]
    assert assistant["citations"], "citations 应随 assistant 消息落库"
