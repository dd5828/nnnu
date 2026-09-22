"""知识库 REST 单测（§9.1）：全端点走一遍 + 错误信封 + 审计落账。

用 client 夹具跑真实 lifespan（KB 服务真的装配了），嵌入换成离线替身——
否则测试会去下载 95MB 的本地模型。
"""

import asyncio
from pathlib import Path

import pymupdf
import pytest
from conftest import HashEmbedder

from nnnu.api.routers import knowledge as knowledge_router
from nnnu.services.audit import read_audit
from nnnu.services.embedding import service as embedding_service

TEXT = "傅里叶变换把时域信号分解为频域分量之和。滤波器设计关注通带与阻带。"


@pytest.fixture(autouse=True)
def _stub_embedder():
    """离线替身：不装的话本地 ONNX 路径会真去下模型。"""
    embedding_service.install_embedding_stub(lambda: HashEmbedder())
    yield
    embedding_service.uninstall_embedding_stub()


def _pdf(path: Path, pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for index in range(1, pages + 1):
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(60, 60, 540, 700),
            f"第{index}页 {TEXT}" * 6,
            fontname="china-s",
            fontsize=9,
        )
    doc.save(str(path))
    return path.read_bytes()


async def _create(client, name="信号处理") -> dict:
    response = await client.post("/api/v1/kbs", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


async def _upload(client, kb_id: str, filename: str, content: bytes, mime: str) -> dict:
    response = await client.post(
        f"/api/v1/kbs/{kb_id}/docs",
        files={"file": (filename, content, mime)},
    )
    return response


async def _wait_ready(client, kb_id: str, *, timeout: float = 20.0) -> dict:
    """轮询到库 ready（构建是后台任务）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        manifest = (await client.get(f"/api/v1/kbs/{kb_id}")).json()
        if manifest["status"] in ("ready", "error"):
            return manifest
        await asyncio.sleep(0.02)
    raise AssertionError("等待索引就绪超时")


async def test_kb_crud_round_trip(client):
    created = await _create(client)
    assert created["status"] == "ready"
    assert created["active_version"] == 0
    kb_id = created["id"]

    listed = (await client.get("/api/v1/kbs")).json()["kbs"]
    assert [item["id"] for item in listed] == [kb_id]

    single = await client.get(f"/api/v1/kbs/{kb_id}")
    assert single.json()["name"] == "信号处理"

    assert (await client.delete(f"/api/v1/kbs/{kb_id}")).json() == {"deleted": kb_id}
    assert (await client.get(f"/api/v1/kbs/{kb_id}")).status_code == 404
    assert (await client.delete(f"/api/v1/kbs/{kb_id}")).status_code == 404
    assert (await client.get("/api/v1/kbs")).json()["kbs"] == []

    actions = [entry["action"] for entry in read_audit()]
    assert actions == ["kb_create", "kb_delete"]


async def test_create_rejects_bad_name(client):
    response = await client.post("/api/v1/kbs", json={"name": "a/b"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_name"


async def test_upload_doc_then_search(client, tmp_path):
    kb_id = (await _create(client))["id"]
    upload = await _upload(
        client, kb_id, "信号讲义.pdf", _pdf(tmp_path / "a.pdf", 2), "application/pdf"
    )
    assert upload.status_code == 200, upload.text
    doc = upload.json()
    assert doc["status"] in ("parsing", "chunking", "embedding", "done")
    assert doc["size"] > 0 and doc["fingerprint"].startswith("sha256:")

    manifest = await _wait_ready(client, kb_id)
    assert manifest["status"] == "ready"
    assert manifest["active_version"] == 1

    detail = (await client.get(f"/api/v1/kbs/{kb_id}/docs/{doc['doc_id']}")).json()
    assert detail["status"] == "done"
    assert detail["page_count"] == 2
    assert detail["chunk_count"] > 0
    assert detail["progress"] == 1.0

    hits = (
        await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "傅里叶变换 滤波器"})
    ).json()
    assert hits["mode"] == "hybrid"
    assert hits["hits"]
    top = hits["hits"][0]
    assert top["doc_id"] == doc["doc_id"]
    assert top["page"] in (1, 2)
    assert top["metadata"]["filename"] == "信号讲义.pdf"
    assert top["metadata"]["kb_name"] == "信号处理"

    vector = await client.post(
        f"/api/v1/kbs/{kb_id}/search", json={"query": "滤波器", "mode": "vector", "top_k": 3}
    )
    assert vector.status_code == 200
    assert all(hit["doc_id"] == doc["doc_id"] for hit in vector.json()["hits"])

    actions = [entry["action"] for entry in read_audit()]
    assert actions == ["kb_create", "kb_doc_add"]


async def test_upload_rejections(client, tmp_path, monkeypatch):
    kb_id = (await _create(client))["id"]

    bad_type = await _upload(client, kb_id, "图.png", b"\x89PNG", "image/png")
    assert bad_type.status_code == 422
    assert bad_type.json()["error"]["code"] == "invalid_request"

    monkeypatch.setattr(knowledge_router, "MAX_KB_UPLOAD_BYTES", 8)
    too_big = await _upload(client, kb_id, "大.txt", b"0123456789", "text/plain")
    assert too_big.status_code == 422
    assert too_big.json()["error"]["code"] == "doc_too_large"

    missing = await _upload(client, "kb-ffffffff", "x.txt", b"hello", "text/plain")
    assert missing.status_code == 404


async def test_error_doc_and_single_delete(client, tmp_path):
    kb_id = (await _create(client))["id"]
    bad = (await _upload(client, kb_id, "坏.pdf", b"%PDF-1.4 nope", "application/pdf")).json()
    good = (
        await _upload(client, kb_id, "好.pdf", _pdf(tmp_path / "ok.pdf"), "application/pdf")
    ).json()
    manifest = await _wait_ready(client, kb_id)

    assert manifest["status"] == "ready"  # 坏文档不阻塞整库（验收 D）
    statuses = {doc["doc_id"]: doc["status"] for doc in manifest["docs"]}
    assert statuses[bad["doc_id"]] == "error"
    assert statuses[good["doc_id"]] == "done"

    assert (await client.delete(f"/api/v1/kbs/{kb_id}/docs/{bad['doc_id']}")).json() == {
        "deleted": bad["doc_id"]
    }
    # 重复删同一个文档是幂等的（前端连点两下不该报错）；文档压根不存在才 404
    assert (await client.delete(f"/api/v1/kbs/{kb_id}/docs/{bad['doc_id']}")).status_code == 200
    assert (await client.delete(f"/api/v1/kbs/{kb_id}/docs/kbdoc-00000000")).status_code == 404

    hits = (await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "傅里叶变换"})).json()
    assert {hit["doc_id"] for hit in hits["hits"]} == {good["doc_id"]}
    # 解析失败的文档没有解析缓存
    assert (
        await client.get(f"/api/v1/kbs/{kb_id}/docs/{bad['doc_id']}/content")
    ).status_code == 404


async def test_doc_file_and_content(client, tmp_path):
    kb_id = (await _create(client))["id"]
    doc = (
        await _upload(client, kb_id, "信号.pdf", _pdf(tmp_path / "a.pdf"), "application/pdf")
    ).json()
    await _wait_ready(client, kb_id)

    file = await client.get(f"/api/v1/kbs/{kb_id}/docs/{doc['doc_id']}/file")
    assert file.status_code == 200
    assert file.headers["content-type"] == "application/pdf"
    assert file.content.startswith(b"%PDF")

    content = (await client.get(f"/api/v1/kbs/{kb_id}/docs/{doc['doc_id']}/content")).json()
    assert content["filename"] == "信号.pdf"
    assert content["page_count"] == 1
    assert "傅里叶变换" in content["text"]
    assert (await client.get(f"/api/v1/kbs/{kb_id}/docs/kbdoc-00000000/file")).status_code == 404


async def test_search_validation(client, tmp_path):
    kb_id = (await _create(client))["id"]
    await _upload(client, kb_id, "信号.pdf", _pdf(tmp_path / "a.pdf"), "application/pdf")
    await _wait_ready(client, kb_id)

    assert (
        await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "x", "mode": "挖矿"})
    ).status_code == 422
    assert (
        await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "   "})
    ).status_code == 422
    assert (
        await client.post("/api/v1/kbs/kb-ffffffff/search", json={"query": "x"})
    ).status_code == 404


async def test_reindex_and_cancel(client, tmp_path):
    kb_id = (await _create(client))["id"]
    await _upload(client, kb_id, "信号.pdf", _pdf(tmp_path / "a.pdf", 3), "application/pdf")
    await _wait_ready(client, kb_id)

    reindexed = await client.post(f"/api/v1/kbs/{kb_id}/reindex", json={"force": False})
    assert reindexed.status_code == 200
    assert reindexed.json()["status"] in ("indexing", "ready")

    manifest = await _wait_ready(client, kb_id)
    assert manifest["active_version"] == 1  # 没有失败的文档，重试无事可做

    # 没有在跑的构建时取消 → 409
    assert (await client.post(f"/api/v1/kbs/{kb_id}/build/cancel")).status_code == 409
    assert (await client.post("/api/v1/kbs/kb-ffffffff/reindex")).status_code == 404


async def test_sources_not_implemented(client):
    kb_id = (await _create(client))["id"]
    for method in ("get", "put"):
        response = await getattr(client, method)(f"/api/v1/kbs/{kb_id}/sources")
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "not_implemented"


async def test_unknown_kb_endpoints_404(client):
    for path in (
        "/api/v1/kbs/kb-ffffffff",
        "/api/v1/kbs/kb-ffffffff/docs/kbdoc-00000000",
        "/api/v1/kbs/kb-ffffffff/docs/kbdoc-00000000/file",
        "/api/v1/kbs/kb-ffffffff/docs/kbdoc-00000000/content",
    ):
        assert (await client.get(path)).status_code == 404, path
    assert (await client.delete("/api/v1/kbs/kb-ffffffff")).status_code == 404
    assert (await client.delete("/api/v1/kbs/kb-ffffffff/docs/kbdoc-00000000")).status_code == 404
