"""附件（§7.1）：上传/下载/删除、MIME 校验、PDF 页级解析、注入与引用。"""

import json
from pathlib import Path

import fitz

from nnnu.services.files.service import MAX_UPLOAD_BYTES
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

# 1x1 透明 PNG
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63f8ffff3f030005fe02fea7e2159b"
    "0000000049454e44ae426082"
)


def _pdf_bytes(pages_text: list[str]) -> bytes:
    """用 PyMuPDF 生成测试 PDF。"""
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    raw = doc.tobytes()
    doc.close()
    return raw


async def _upload(client, filename: str, content: bytes, mime: str, session_id: str = "sess-att"):
    return await client.post(
        "/api/v1/attachments",
        files={"file": (filename, content, mime)},
        data={"session_id": session_id},
    )


async def test_upload_list_download_delete(client, tmp_home):
    resp = await _upload(client, "笔记.txt", "这是附件内容".encode("utf-8"), "text/plain")
    assert resp.status_code == 200
    record = resp.json()
    assert record["id"].startswith("att-")
    assert record["mime"] == "text/plain"
    assert record["kind"] == "text"

    listed = await client.get("/api/v1/attachments", params={"session_id": "sess-att"})
    assert [a["id"] for a in listed.json()["attachments"]] == [record["id"]]

    downloaded = await client.get(f"/api/v1/attachments/{record['id']}")
    assert downloaded.status_code == 200
    assert downloaded.content.decode("utf-8") == "这是附件内容"

    # 解析缓存落盘（能力注入的磁盘契约）
    parsed = tmp_home / "data" / "user" / "uploads" / "sess-att" / record["id"] / "parsed.json"
    assert json.loads(parsed.read_text(encoding="utf-8"))["text"] == "这是附件内容"

    deleted = await client.delete(f"/api/v1/attachments/{record['id']}")
    assert deleted.status_code == 200
    assert (await client.get(f"/api/v1/attachments/{record['id']}")).status_code == 404
    assert not parsed.parent.exists()  # 归档目录整体清理（§7.1 解析缓存清理入口）


async def test_upload_rejects_unsupported_mime(client):
    resp = await _upload(client, "evil.exe", b"MZ", "application/x-msdownload")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_mime"


async def test_upload_rejects_oversize(client, monkeypatch):
    monkeypatch.setattr("nnnu.services.files.service.MAX_UPLOAD_BYTES", 10)
    resp = await _upload(client, "big.txt", b"x" * 11, "text/plain")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "attachment_too_large"


async def test_upload_extension_fallback_for_code(client):
    # octet-stream + .py 扩展名 → 按代码类文本接受（§11.3 扩展名回退）
    resp = await _upload(client, "main.py", b"print('hi')", "application/octet-stream")
    assert resp.status_code == 200
    assert resp.json()["mime"] == "text/plain"


async def test_pdf_upload_parses_pages(client, tmp_home):
    resp = await _upload(
        client,
        "doc.pdf",
        _pdf_bytes(["First page content", "Second page content"]),
        "application/pdf",
    )
    assert resp.status_code == 200
    record = resp.json()
    parsed_path = tmp_home / "data" / "user" / "uploads" / "sess-att" / record["id"] / "parsed.json"
    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
    assert parsed["page_count"] == 2
    assert [p["page"] for p in parsed["pages"]] == [1, 2]
    assert "First page content" in parsed["pages"][0]["text"]


async def test_chat_with_attachment_cites_pages(client):
    """端到端验收②：上传 PDF → 提问 → attachment_search 命中 → citation 带页码。"""
    upload = await _upload(
        client,
        "textbook.pdf",
        _pdf_bytes(
            [
                "Fourier transform decomposes a signal into sine waves of different frequencies."
                + "." * 80,
                "Appendix",
            ]
        ),
        "application/pdf",
    )
    attachment = upload.json()

    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM(
            [
                ScriptedStep(
                    tool_calls=[
                        LLMToolCall(
                            id="c1",
                            name="attachment_search",
                            arguments=json.dumps({"query": "Fourier transform"}),
                        )
                    ]
                ),
                ScriptedStep(chunks=["Page 1 of the textbook explains the Fourier transform:"]),
            ]
        )
        return captured

    install_scripted(factory)
    try:
        chat = await client.post(
            "/api/v1/chat",
            json={
                "session_id": "sess-att",
                "message": "How does the textbook explain the Fourier transform?",
                "attachments": [{"id": attachment["id"], "name": "textbook.pdf"}],
            },
        )
    finally:
        uninstall_scripted()
    assert chat.status_code == 200
    citations = chat.json()["citations"]
    assert citations, "attachment_search 命中应转 citation"
    assert citations[0]["doc_id"] == attachment["id"]
    assert citations[0]["kb"] == "attachment"
    assert citations[0]["page"] == 1

    # 工具随附件自动挂载（§6.3 context_gated）
    tool_names = [t["function"]["name"] for t in captured.calls[0].tools]
    assert "attachment_search" in tool_names

    # 附件正文带页码标记注入用户消息（按 role 取：messages 是可变引用，回合中会被追加）
    user_content = next(m["content"] for m in captured.calls[0].messages if m["role"] == "user")
    assert "【附件《textbook.pdf》" in user_content
    assert "[第1页]" in user_content


async def test_chat_without_attachment_does_not_mount_search(client):
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        await client.post("/api/v1/chat", json={"session_id": "sess-noatt", "message": "问题"})
    finally:
        uninstall_scripted()
    tool_names = [t["function"]["name"] for t in captured.calls[0].tools]
    assert "attachment_search" not in tool_names


async def test_too_many_attachments_rejected(client):
    attachments = [{"id": f"att-{i}", "name": f"{i}.txt"} for i in range(6)]
    chat = await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-many", "message": "问题", "attachments": attachments},
    )
    assert chat.status_code == 422


async def test_image_ignored_without_vision_model(client):
    """默认 deepseek-chat 无视觉能力 → 图片附件不注入（content 保持纯文本）。"""
    upload = await _upload(client, "图.png", PNG_1PX, "image/png")
    attachment = upload.json()

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
                "session_id": "sess-att",
                "message": "看图",
                "attachments": [{"id": attachment["id"], "name": "图.png", "mime": "image/png"}],
            },
        )
    finally:
        uninstall_scripted()
    user_content = next(m["content"] for m in captured.calls[0].messages if m["role"] == "user")
    assert isinstance(user_content, str)
    assert "image_url" not in user_content


async def test_image_injected_for_vision_model(client):
    upload = await _upload(client, "图.png", PNG_1PX, "image/png")
    attachment = upload.json()

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
                "session_id": "sess-att",
                "message": "看图",
                "model": "openai:gpt-4o",
                "attachments": [{"id": attachment["id"], "name": "图.png", "mime": "image/png"}],
            },
        )
    finally:
        uninstall_scripted()
    user_content = next(m["content"] for m in captured.calls[0].messages if m["role"] == "user")
    assert isinstance(user_content, list)
    image_part = next(p for p in user_content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_bm25_ranking_prefers_query_terms():
    from nnnu.tools.builtin.attachment_search import _bm25_search

    docs = [
        {"att_id": "a1", "name": "甲", "page": 1, "text": "傅里叶变换 傅里叶变换 傅里叶变换"},
        {"att_id": "a2", "name": "乙", "page": 2, "text": "完全无关的内容"},
        {"att_id": "a3", "name": "丙", "page": 3, "text": "傅里叶变换"},
    ]
    hits = _bm25_search("傅里叶变换", docs, top_k=2)
    assert [h["att_id"] for h in hits] == ["a1", "a3"]
