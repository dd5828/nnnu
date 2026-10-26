"""P14 外部库：Obsidian 原位读写、IMA OpenAPI 浏览/检索/写回、MarginNote 接收端。

IMA 不碰真网络：httpx.MockTransport 同时供普通请求与素材下载使用；MarginNote
的令牌、幂等 upsert、墓碑删除都在本地 JSON 上验证。Obsidian 用真实临时目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.external_libs.ima import ImaAuthError, ImaClient, ImaConfig, ImaError


@pytest.fixture
async def app_client(tmp_home):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "MyVault"
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (root / "学习").mkdir()
    (root / "学习" / "傅里叶.md").write_text(
        "# 傅里叶\n\n时域信号可以分解成频域分量。\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# 首页\n\n索引页\n", encoding="utf-8")
    (root / "图片.png").write_bytes(b"\x89PNG")
    return root


# ================= Obsidian =================


async def test_obsidian_mount_list_read_write_roundtrip(app_client, tmp_path):
    app, client = app_client
    root = _vault(tmp_path)
    added = await client.post(
        "/api/v1/libraries/obsidian/vaults", json={"path": str(root), "name": "我的库"}
    )
    assert added.status_code == 200, added.text
    vault = added.json()["vault"]
    assert vault["name"] == "我的库" and vault["vault_id"].startswith("vault-")

    listing = (
        await client.get(f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}/notes")
    ).json()
    paths = {row["path"] for row in listing["notes"]}
    assert paths == {"README.md", "学习/傅里叶.md"}  # .obsidian 与 png 不列

    only_folder = (
        await client.get(
            f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}/notes",
            params={"folder": "学习"},
        )
    ).json()
    assert [row["path"] for row in only_folder["notes"]] == ["学习/傅里叶.md"]

    note = (
        await client.get(
            f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}/note",
            params={"path": "学习/傅里叶"},
        )
    ).json()
    assert "频域" in note["content"] and note["path"] == "学习/傅里叶.md"

    written = await client.put(
        f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}/note",
        json={"path": "学习/新笔记", "content": "# 新笔记\n\n写回成功。", "mode": "overwrite"},
    )
    assert written.status_code == 200, written.text
    assert written.json()["path"] == "学习/新笔记.md" and written.json()["created"] is True
    assert (root / "学习" / "新笔记.md").read_text(encoding="utf-8") == "# 新笔记\n\n写回成功。"

    appended = await client.put(
        f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}/note",
        json={"path": "学习/新笔记.md", "content": "追加一行。", "mode": "append"},
    )
    assert appended.status_code == 200
    assert (root / "学习" / "新笔记.md").read_text(encoding="utf-8").endswith("追加一行。")

    search = (
        await client.get(
            f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}/search",
            params={"q": "频域"},
        )
    ).json()
    assert search["hits"] and search["hits"][0]["path"] == "学习/傅里叶.md"

    removed = await client.delete(f"/api/v1/libraries/obsidian/vaults/{vault['vault_id']}")
    assert removed.status_code == 200
    assert (root / "README.md").exists()  # 移除挂载不动原文件
    assert (await client.get("/api/v1/libraries/obsidian/vaults")).json()["vaults"] == []


async def test_obsidian_path_escape_and_bad_vaults_are_rejected(app_client, tmp_path):
    app, client = app_client
    root = _vault(tmp_path)
    vault = (
        await client.post("/api/v1/libraries/obsidian/vaults", json={"path": str(root)})
    ).json()["vault"]
    vault_id = vault["vault_id"]

    for path in ("../outside.md", "学习/../../outside.md", "C:/Windows/win.ini"):
        read = await client.get(
            f"/api/v1/libraries/obsidian/vaults/{vault_id}/note", params={"path": path}
        )
        assert read.status_code in {404, 422}, (path, read.text)
        write = await client.put(
            f"/api/v1/libraries/obsidian/vaults/{vault_id}/note",
            json={"path": path, "content": "x"},
        )
        assert write.status_code == 422, (path, write.text)
    assert not (tmp_path / "outside.md").exists()

    missing = await client.get(
        f"/api/v1/libraries/obsidian/vaults/{vault_id}/note", params={"path": "不存在"}
    )
    assert missing.status_code == 404

    not_dir = await client.post(
        "/api/v1/libraries/obsidian/vaults", json={"path": str(root / "README.md")}
    )
    assert not_dir.status_code == 422
    duplicate = await client.post("/api/v1/libraries/obsidian/vaults", json={"path": str(root)})
    assert duplicate.status_code == 422
    unknown = await client.get("/api/v1/libraries/obsidian/vaults/vault-ffffffff/notes")
    assert unknown.status_code == 404
    assert (
        await client.get(f"/api/v1/libraries/obsidian/vaults/{vault_id}/note", params={"path": ""})
    ).status_code == 422


# ================= IMA =================


class ImaWire:
    """一个按路径分派的假 IMA 服务端；calls 记录请求体供断言。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.mode = "ok"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        if self.mode == "auth":
            return httpx.Response(200, json={"retcode": 20004, "errmsg": "bad key"})
        if self.mode == "rate":
            return httpx.Response(429, json={"retcode": 20002})
        if path.endswith("/get_knowledge_base"):
            return httpx.Response(
                200,
                json={"retcode": 0, "data": {"infos": {"kb-1": {"name": "我的 IMA 库"}}}},
            )
        if path.endswith("/get_knowledge_list"):
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {
                        "knowledge_list": [
                            {"media_id": "m1", "title": "傅里叶讲义"},
                            {"folder_id": "f1", "name": "课程"},
                        ],
                        "folder_list": [{"folder_id": "f2", "name": "论文", "file_number": 3}],
                        "current_path": [{"name": "根目录"}],
                        "next_cursor": "c1",
                        "is_end": False,
                    },
                },
            )
        if path.endswith("/search_knowledge"):
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {
                        "info_list": [
                            {
                                "media_id": "m2",
                                "title": "滤波器",
                                "highlight_content": "通带与阻带",
                            }
                        ],
                        "is_end": True,
                    },
                },
            )
        if path.endswith("/get_media_info"):
            if body.get("media_id") == "m-note":
                return httpx.Response(
                    200,
                    json={
                        "retcode": 0,
                        "data": {
                            "title": "我的笔记",
                            "media_type": 11,
                            "notebook_ext_info": {"notebook_id": "n1"},
                        },
                    },
                )
            if body.get("media_id") == "m-file":
                return httpx.Response(
                    200,
                    json={
                        "retcode": 0,
                        "data": {
                            "title": "a.txt",
                            "url_info": {"url": "https://bucket.cos.myqcloud.com/a.txt"},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {
                        "title": "evil",
                        "url_info": {"url": "https://evil.example/a.txt"},
                    },
                },
            )
        if path == "/openapi/note/v1/get_doc_content":
            return httpx.Response(200, json={"retcode": 0, "data": {"content": "笔记正文"}})
        if path == "/openapi/note/v1/search_note_book":
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {
                        "docs": [
                            {
                                "doc": {
                                    "basic_info": {
                                        "docid": "n1",
                                        "title": "学习笔记",
                                        "summary": "摘要",
                                        "folder_name": "默认",
                                        "create_time": 1,
                                        "modify_time": 2,
                                    }
                                }
                            }
                        ],
                        "is_end": True,
                    },
                },
            )
        if path == "/openapi/note/v1/import_doc":
            return httpx.Response(200, json={"retcode": 0, "data": {"doc_id": "n2"}})
        if path == "/openapi/note/v1/append_doc":
            return httpx.Response(200, json={"retcode": 0, "data": {"doc_id": "n1"}})
        if path.endswith("/import_urls"):
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {
                        "url_data_list": [
                            {"url": "https://example.com/a", "ret_code": 0, "media_id": "m9"}
                        ]
                    },
                },
            )
        if request.url.host == "bucket.cos.myqcloud.com":
            return httpx.Response(200, text="文件里的正文", headers={"content-type": "text/plain"})
        return httpx.Response(404, json={"retcode": 404, "errmsg": "no such method"})


def _use_ima(app, wire: ImaWire) -> None:
    app.state.ima._client_factory = lambda config: ImaClient(
        config, transport=httpx.MockTransport(wire.handler)
    )


@pytest.fixture
def ima_env(monkeypatch):
    monkeypatch.setenv("NNNU_IMA_CLIENT_ID", "cid")
    monkeypatch.setenv("NNNU_IMA_API_KEY", "secret")
    monkeypatch.setenv("NNNU_IMA_KB_ID", "kb-1")


async def test_ima_status_and_browse_search_read(app_client, ima_env):
    app, client = app_client
    wire = ImaWire()
    _use_ima(app, wire)

    status = (await client.get("/api/v1/libraries/ima/status")).json()
    assert status == {"configured": True, "knowledge_base_id": "kb-1", "error": ""}
    test = (await client.post("/api/v1/libraries/ima/test")).json()
    assert test == {"ok": True, "name": "我的 IMA 库", "error": ""}

    browse = (await client.get("/api/v1/libraries/ima/browse")).json()
    assert browse["path"] == ["根目录"]
    assert [item["title"] for item in browse["documents"]] == ["傅里叶讲义"]
    assert {item["folder_id"] for item in browse["folders"]} == {"f1", "f2"}
    assert browse["next_cursor"] == "c1" and browse["is_end"] is False

    search = (await client.get("/api/v1/libraries/ima/search", params={"q": "滤波"})).json()
    assert search["documents"][0]["highlight"] == "通带与阻带"

    note = (await client.get("/api/v1/libraries/ima/items/m-note")).json()
    assert note["kind"] == "note" and note["text"] == "笔记正文"
    file_item = (await client.get("/api/v1/libraries/ima/items/m-file")).json()
    assert file_item["kind"] == "file" and file_item["text"] == "文件里的正文"
    bad_url = (await client.get("/api/v1/libraries/ima/items/m-evil")).json()
    assert "COS" in bad_url["error"]["message"]

    notes = (await client.get("/api/v1/libraries/ima/notes", params={"q": "学习"})).json()
    assert notes["notes"][0]["note_id"] == "n1" and notes["notes"][0]["title"] == "学习笔记"

    created = (
        await client.post("/api/v1/libraries/ima/notes", json={"content": "# 新笔记\n正文"})
    ).json()
    assert created == {"note_id": "n2", "action": "created"}
    appended = (
        await client.post("/api/v1/libraries/ima/notes/n1/append", json={"content": "补一句。"})
    ).json()
    assert appended == {"note_id": "n1", "action": "appended"}

    urls = (
        await client.post("/api/v1/libraries/ima/urls", json={"urls": ["https://example.com/a"]})
    ).json()
    assert urls["added"][0]["media_id"] == "m9" and urls["failed"] == []


async def test_ima_errors_are_mapped(app_client, ima_env):
    app, client = app_client
    wire = ImaWire()
    _use_ima(app, wire)

    wire.mode = "auth"
    auth = await client.get("/api/v1/libraries/ima/browse")
    assert auth.status_code == 502 and auth.json()["error"]["code"] == "ima_auth"
    wire.mode = "rate"
    rate = await client.get("/api/v1/libraries/ima/browse")
    assert rate.status_code == 429 and rate.json()["error"]["code"] == "ima_rate_limited"
    wire.mode = "ok"

    bad = await client.post("/api/v1/libraries/ima/urls", json={"urls": ["https://youtu.be/x"]})
    assert bad.status_code == 422
    empty = await client.get("/api/v1/libraries/ima/search", params={"q": ""})
    assert empty.status_code == 422


async def test_ima_without_credentials_says_what_is_missing(app_client, monkeypatch):
    app, client = app_client
    for name in ("NNNU_IMA_CLIENT_ID", "NNNU_IMA_API_KEY", "NNNU_IMA_KB_ID"):
        monkeypatch.delenv(name, raising=False)
    status = (await client.get("/api/v1/libraries/ima/status")).json()
    assert status["configured"] is False and "client ID" in status["error"]
    browse = await client.get("/api/v1/libraries/ima/browse")
    assert browse.status_code == 422 and browse.json()["error"]["code"] == "ima_not_configured"


async def test_ima_client_envelope_and_ssrf_guard():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200002, "msg": "denied"})

    client = ImaClient(
        ImaConfig("c", "k", "kb"),
        transport=httpx.MockTransport(handler),
        base_url="https://ima.test",
    )
    with pytest.raises(ImaAuthError):
        await client.browse()

    def file_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "retcode": 0,
                "data": {
                    "title": "x",
                    "url_info": {"url": "http://bucket.cos.myqcloud.com/a.txt"},
                },
            },
        )

    client = ImaClient(
        ImaConfig("c", "k", "kb"),
        transport=httpx.MockTransport(file_handler),
        base_url="https://ima.test",
    )
    with pytest.raises(ImaError, match="HTTPS"):
        await client.read_item("m1")


# ================= MarginNote =================


def _mn_object(object_id: str = "o1", **overrides):
    payload = {
        "object_id": object_id,
        "object_type": "note",
        "title": "傅里叶摘录",
        "content": "时域到频域",
        "excerpt": "时域信号可以分解",
        "document_id": "d1",
        "document_title": "信号与系统",
        "page": 12,
        "tags": ["信号", "数学"],
        "links": ["o2"],
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        "raw": {"mn": True},
    }
    payload.update(overrides)
    return payload


async def test_marginnote_pair_sync_browse_and_revoke(app_client):
    app, client = app_client
    paired = await client.post(
        "/api/v1/libraries/marginnote/pair",
        json={"device_name": "iPad", "device_kind": "ipados"},
    )
    assert paired.status_code == 200, paired.text
    payload = paired.json()
    device_id, token = payload["device"]["device_id"], payload["token"]
    assert payload["device"]["device_name"] == "iPad"
    devices = (await client.get("/api/v1/libraries/marginnote/devices")).json()["devices"]
    assert devices and "token" not in devices[0] and devices[0]["device_id"] == device_id

    no_token = await client.post("/api/v1/libraries/marginnote/sync", json={"objects": []})
    assert no_token.status_code == 401
    wrong = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"objects": []},
        headers={"Authorization": f"MarginNote {device_id}:wrong"},
    )
    assert wrong.status_code == 403

    first = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"cursor": "1", "objects": [_mn_object()]},
        headers={"Authorization": f"MarginNote {device_id}:{token}"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"stored": 1, "updated": 0, "deleted": 0, "new_cursor": "1"}

    second = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={
            "objects": [
                _mn_object(title="改过的标题"),
                _mn_object("o2", object_type="card", title="卡片", document_id="d1"),
            ]
        },
        headers={"Authorization": f"MarginNote {device_id}:{token}"},
    )
    assert second.json()["stored"] == 1 and second.json()["updated"] == 1

    listing = (await client.get("/api/v1/libraries/marginnote/objects")).json()
    assert listing["count"] == 2
    search = (await client.get("/api/v1/libraries/marginnote/objects", params={"q": "改过"})).json()
    assert search["results"][0]["object_id"] == "o1"

    read = (await client.get("/api/v1/libraries/marginnote/objects/o1")).json()
    assert read["title"] == "改过的标题" and read["device_id"] == device_id
    links = (await client.get("/api/v1/libraries/marginnote/objects/o1/links")).json()
    assert {row["object_id"] for row in links["links"]} == {"o2"}
    docs = (await client.get("/api/v1/libraries/marginnote/documents")).json()
    assert docs["documents"][0]["document_id"] == "d1" and docs["documents"][0]["objects"] == 2
    tags = (await client.get("/api/v1/libraries/marginnote/tags")).json()
    assert tags["tags"][0] == {"tag": "信号", "count": 2}

    deleted = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"deleted_ids": ["o2"]},
        headers={"Authorization": f"MarginNote {device_id}:{token}"},
    )
    assert deleted.json()["deleted"] == 1
    assert (await client.get("/api/v1/libraries/marginnote/objects/o2")).status_code == 404

    revoked = await client.delete(f"/api/v1/libraries/marginnote/devices/{device_id}")
    assert revoked.status_code == 200 and revoked.json()["device"]["active"] is False
    blocked = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"objects": []},
        headers={"Authorization": f"MarginNote {device_id}:{token}"},
    )
    assert blocked.status_code == 403


async def test_marginnote_rejects_bad_batches(app_client):
    app, client = app_client
    paired = (
        await client.post("/api/v1/libraries/marginnote/pair", json={"device_name": "Mac"})
    ).json()
    headers = {"Authorization": f"MarginNote {paired['device']['device_id']}:{paired['token']}"}
    unknown_type = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"objects": [_mn_object(object_type="nope")]},
        headers=headers,
    )
    assert unknown_type.status_code == 422
    too_many_tags = await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"objects": [_mn_object(tags=[f"t{i}" for i in range(101)])]},
        headers=headers,
    )
    assert too_many_tags.status_code == 422


# ================= 工具与挂载门 =================


async def test_external_lib_tools_are_gated_and_work(app_client, tmp_path, ima_env):
    from nnnu.capabilities._shared import (
        _ima_gate,
        _marginnote_gate,
        _obsidian_gate,
    )
    from nnnu.tools.builtin.external_lib_tools import (
        ImaSearchTool,
        MarginNoteSearchTool,
        ObsidianListTool,
        ObsidianReadTool,
    )

    app, client = app_client
    # IMA 凭据由 ima_env 提供，门本来就是开的；Obsidian/MarginNote 还没有挂载/配对
    assert not _obsidian_gate() and not _marginnote_gate() and _ima_gate()

    root = _vault(tmp_path)
    await client.post("/api/v1/libraries/obsidian/vaults", json={"path": str(root)})
    assert _obsidian_gate()

    ctx = ToolContext(turn_id="t", session_id="s", args={})
    listed = await ObsidianListTool().run(ctx)
    assert listed.ok and "学习/傅里叶.md" in listed.output
    read = await ObsidianReadTool().run(
        ToolContext(turn_id="t", session_id="s", args={"path": "学习/傅里叶"})
    )
    assert read.ok and "频域" in read.output

    wire = ImaWire()
    _use_ima(app, wire)
    assert _ima_gate()
    searched = await ImaSearchTool().run(
        ToolContext(turn_id="t", session_id="s", args={"query": "滤波"})
    )
    assert searched.ok and "滤波器" in searched.output

    paired = (
        await client.post("/api/v1/libraries/marginnote/pair", json={"device_name": "Mac"})
    ).json()
    headers = {"Authorization": f"MarginNote {paired['device']['device_id']}:{paired['token']}"}
    await client.post(
        "/api/v1/libraries/marginnote/sync",
        json={"objects": [_mn_object()]},
        headers=headers,
    )
    assert _marginnote_gate()
    found = await MarginNoteSearchTool().run(
        ToolContext(turn_id="t", session_id="s", args={"query": "傅里叶"})
    )
    assert found.ok and "o1" in found.output
