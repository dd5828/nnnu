"""GitHub 源同步（§7.9 P14）：漂移检查、增量同步、删除、错配与鉴权提示。

全部走假客户端（FakeGithub），不碰真网络；真实 REST 细节由 client 单测另测。
"""

import httpx
import pytest
from conftest import HashEmbedder
from httpx import ASGITransport, AsyncClient

from nnnu.services.embedding import service as embedding_service
from nnnu.services.github_source.client import (
    GithubClient,
    GithubConfigError,
    GithubError,
    TreeFile,
    resolve_github_token,
    validate_repo,
)
from nnnu.services.github_source.service import compile_glob


class FakeGithubState:
    def __init__(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}
        self.tree_error: Exception | None = None
        self.download_error: Exception | None = None
        self.calls: list[tuple[str, str, str]] = []  # ("list"|"download", repo, path/ref)

    def tree(self) -> list[TreeFile]:
        if self.tree_error is not None:
            raise self.tree_error
        return [
            TreeFile(path=path, sha=sha, size=len(content))
            for path, (sha, content) in self.files.items()
        ]


class FakeGithub:
    def __init__(self, state: FakeGithubState) -> None:
        self._state = state

    async def list_files(self, repo: str, ref: str) -> list[TreeFile]:
        self._state.calls.append(("list", repo, ref))
        return self._state.tree()

    async def download(self, repo: str, ref: str, path: str) -> bytes:
        self._state.calls.append(("download", repo, path))
        if self._state.download_error is not None:
            raise self._state.download_error
        return self._state.files[path][1]

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _stub_embedder():
    embedding_service.install_embedding_stub(lambda: HashEmbedder())
    yield
    embedding_service.uninstall_embedding_stub()


@pytest.fixture
async def app_client(tmp_home, monkeypatch):
    from nnnu.api.main import create_app

    app = create_app()
    state = FakeGithubState()
    async with app.router.lifespan_context(app):
        app.state.github_sources._client_factory = lambda: FakeGithub(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c, state


# ================= glob =================


def test_glob_matches_root_and_nested():
    matcher = compile_glob("docs/**/*.md")
    assert matcher.match("docs/intro.md")
    assert matcher.match("docs/a/b/c.md")
    assert not matcher.match("docs/a/b/c.txt")
    assert not matcher.match("other/intro.md")

    flat = compile_glob("*.md")
    assert flat.match("README.md")
    assert not flat.match("docs/intro.md")  # `*` 不跨目录

    single = compile_glob("docs/?.md")
    assert single.match("docs/a.md")
    assert not single.match("docs/ab.md")


def test_repo_validation():
    assert validate_repo("owner/repo") == "owner/repo"
    assert validate_repo("/OpenAI/whisper/") == "OpenAI/whisper"
    for bad in ("repo", "a/b/c", "own er/repo", "owner/re po", "/"):
        with pytest.raises(GithubConfigError):
            validate_repo(bad)


def test_token_env_fallback(monkeypatch):
    monkeypatch.setattr(
        "nnnu.services.github_source.client.get_secrets_store",
        lambda: type("S", (), {"get": lambda self, domain, slot: None})(),
    )
    monkeypatch.delenv("NNNU_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert resolve_github_token() is None
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    assert resolve_github_token() == "env-token"
    monkeypatch.setenv("NNNU_GITHUB_TOKEN", "nnnu-token")
    assert resolve_github_token() == "nnnu-token"  # 专用变量优先

    monkeypatch.setattr(
        "nnnu.services.github_source.client.get_secrets_store",
        lambda: type("S", (), {"get": lambda self, domain, slot: "stored-token"})(),
    )
    assert resolve_github_token() == "stored-token"  # 设置里的优先于环境变量


async def test_client_tree_and_raw_download():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/trees/main"):
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"path": "a.md", "type": "blob", "sha": "s1", "size": 5},
                        {"path": "docs", "type": "tree", "sha": "t1"},
                    ],
                },
            )
        if request.url.host == "raw.test":
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(200, content=b"hello")
        return httpx.Response(404, json={"message": "Not Found"})

    client = GithubClient(
        "tok",
        transport=httpx.MockTransport(handler),
        api_base="https://api.test",
        raw_base="https://raw.test",
    )
    try:
        files = await client.list_files("o/r", "main")
        assert [(item.path, item.sha, item.size) for item in files] == [("a.md", "s1", 5)]
        assert await client.download("o/r", "main", "docs/a.md") == b"hello"
    finally:
        await client.aclose()


async def test_client_error_messages():
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.split("/")[-1]
        if name == "forbidden":
            return httpx.Response(404, json={"message": "Not Found"})
        if name == "limited":
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0"},
            )
        if name == "private":
            return httpx.Response(403, json={"message": "Resource not accessible"})
        if name == "expired":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"truncated": True, "tree": []})

    async def call(ref: str) -> str:
        client = GithubClient(
            "tok", transport=httpx.MockTransport(handler), api_base="https://api.test"
        )
        try:
            await client.list_files("o/r", ref)
        except GithubError as exc:
            return str(exc)
        finally:
            await client.aclose()
        raise AssertionError("预期抛 GithubError")

    assert "不存在" in await call("forbidden")
    assert "额度用完" in await call("limited")
    assert "没有权限" in await call("private")
    assert "token 无效" in await call("expired")
    assert "只返回了一部分" in await call("truncated")


async def test_download_rejects_oversized_file():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"tiny", headers={"content-length": str(300 * 1024 * 1024)}
        )

    client = GithubClient(
        "tok", transport=httpx.MockTransport(handler), raw_base="https://raw.test"
    )
    try:
        with pytest.raises(GithubError, match="200MB"):
            await client.download("o/r", "main", "big.pdf")
    finally:
        await client.aclose()


# ================= 源同步主流程 =================


async def test_add_source_syncs_and_redownloads_only_changed(app_client):
    app, client, state = app_client
    kb = (await client.post("/api/v1/kbs", json={"name": "仓库知识"})).json()
    kb_id = kb["id"]
    state.files = {
        "docs/a.md": ("sha-a1", "# 傅里叶\n\n时域信号可以分解成频域分量。".encode()),
        "docs/b.md": ("sha-b1", "# 滤波器\n\n通带与阻带的设计。".encode()),
        "assets/logo.png": ("sha-p1", b"\x89PNG-not-text"),
    }

    added = await client.post(
        f"/api/v1/kbs/{kb_id}/sources",
        json={"repo": "owner/repo", "branch": "main", "glob": "**/*"},
    )
    assert added.status_code == 200, added.text
    payload = added.json()
    assert payload["sync_error"] == ""
    assert payload["sync"]["added"] == ["docs/a.md", "docs/b.md"]
    assert payload["sync"]["docs_created"] == 2
    assert payload["sync"]["skipped"] == ["assets/logo.png"]
    source = payload["source"]
    assert len(source["docs"]) == 2
    first_doc_id = source["docs"]["docs/a.md"]["doc_id"]

    await app.state.kb.wait_idle(kb_id, timeout=30)
    manifest = (await client.get(f"/api/v1/kbs/{kb_id}")).json()
    assert manifest["status"] == "ready"
    assert {doc["source_kind"] for doc in manifest["docs"]} == {"github"}
    assert {doc["source_path"] for doc in manifest["docs"]} == {"docs/a.md", "docs/b.md"}

    hits = (await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "傅里叶"})).json()
    assert hits["hits"] and hits["hits"][0]["metadata"]["filename"] == "a.md"

    # 远端只改一个文件 + 加一个文件：检查只报差异，同步只下载这两个
    state.files["docs/a.md"] = ("sha-a2", "# 傅里叶\n\n新的正文：频谱与相位。".encode())
    state.files["docs/c.md"] = ("sha-c1", "# 采样\n\n奈奎斯特频率。".encode())
    state.calls.clear()

    checked = (await client.post(f"/api/v1/kbs/{kb_id}/sources/{source['source_id']}/check")).json()
    assert checked["added"] == ["docs/c.md"]
    assert checked["modified"] == ["docs/a.md"]
    assert checked["deleted"] == []
    assert checked["unchanged"] == 1
    assert not [call for call in state.calls if call[0] == "download"]  # 检查不下载正文

    synced = (await client.post(f"/api/v1/kbs/{kb_id}/sources/{source['source_id']}/sync")).json()
    downloaded = {call[2] for call in state.calls if call[0] == "download"}
    assert downloaded == {"docs/a.md", "docs/c.md"}  # b.md 的 sha 没变，不重复下载
    assert synced["docs_updated"] == 1
    assert synced["docs_created"] == 1

    await app.state.kb.wait_idle(kb_id, timeout=30)
    manifest = (await client.get(f"/api/v1/kbs/{kb_id}")).json()
    docs_by_path = {doc["source_path"]: doc for doc in manifest["docs"]}
    assert docs_by_path["docs/a.md"]["doc_id"] == first_doc_id  # 原地更新，引用不失效
    assert docs_by_path["docs/a.md"]["remote_sha"] == "sha-a2"
    assert docs_by_path["docs/c.md"]["status"] == "done"

    hits = (await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "频谱 相位"})).json()
    assert hits["hits"] and hits["hits"][0]["metadata"]["filename"] == "a.md"


async def test_sync_deletes_docs_missing_remotely(app_client):
    app, client, state = app_client
    kb_id = (await client.post("/api/v1/kbs", json={"name": "删文档"})).json()["id"]
    state.files = {"a.md": ("s1", "傅里叶 频谱".encode()), "b.md": ("s2", "滤波器".encode())}
    added = (
        await client.post(f"/api/v1/kbs/{kb_id}/sources", json={"repo": "o/r", "glob": "*.md"})
    ).json()
    source_id = added["source"]["source_id"]
    await app.state.kb.wait_idle(kb_id, timeout=30)
    doc_a = added["source"]["docs"]["a.md"]["doc_id"]

    del state.files["a.md"]
    report = (await client.post(f"/api/v1/kbs/{kb_id}/sources/{source_id}/sync")).json()
    assert report["deleted"] == ["a.md"]
    assert report["docs_deleted"] == 1

    manifest = (await client.get(f"/api/v1/kbs/{kb_id}")).json()
    statuses = {doc["doc_id"]: doc["status"] for doc in manifest["docs"]}
    assert statuses[doc_a] == "deleted"
    hits = (await client.post(f"/api/v1/kbs/{kb_id}/search", json={"query": "傅里叶"})).json()
    assert doc_a not in {hit["doc_id"] for hit in hits["hits"]}

    # 解绑并删文档
    assert (await client.delete(f"/api/v1/kbs/{kb_id}/sources/{source_id}")).json() == {
        "deleted": source_id,
        "docs_removed": True,
    }
    assert (await client.get(f"/api/v1/kbs/{kb_id}/sources")).json()["sources"] == []


async def test_source_errors_are_readable(app_client):
    app, client, state = app_client
    kb_id = (await client.post("/api/v1/kbs", json={"name": "错误"})).json()["id"]

    bad_repo = await client.post(f"/api/v1/kbs/{kb_id}/sources", json={"repo": "not a repo"})
    assert bad_repo.status_code == 422
    assert bad_repo.json()["error"]["code"] == "invalid_source"

    bad_glob = await client.post(f"/api/v1/kbs/{kb_id}/sources", json={"repo": "o/r", "glob": ""})
    assert bad_glob.status_code == 422

    missing_kb = await client.get("/api/v1/kbs/kb-ffffffff/sources")
    assert missing_kb.status_code == 404

    state.tree_error = GithubError("私有仓库需要配置 token")
    added = await client.post(
        f"/api/v1/kbs/{kb_id}/sources",
        json={"repo": "o/r", "glob": "*.md", "sync_now": True},
    )
    assert added.status_code == 200
    body = added.json()
    assert "token" in body["sync_error"]
    assert body["sync"] is None
    # 源已保存，错误也记在源上
    sources = (await client.get(f"/api/v1/kbs/{kb_id}/sources")).json()["sources"]
    assert sources and "token" in sources[0]["last_error"]

    state.tree_error = None
    state.download_error = GithubError("文件超过 200MB 上限，已跳过")
    state.files = {"a.md": ("s1", b"x")}
    report = (
        await client.post(f"/api/v1/kbs/{kb_id}/sources/{sources[0]['source_id']}/sync")
    ).json()
    assert report["failed"] and "200MB" in report["failed"][0]["error"]


async def test_interval_and_duplicate_validation(app_client):
    app, client, state = app_client
    kb_id = (await client.post("/api/v1/kbs", json={"name": "校验"})).json()["id"]
    body = {"repo": "o/r", "glob": "*.md", "interval_minutes": 5, "sync_now": False}
    first = await client.post(f"/api/v1/kbs/{kb_id}/sources", json=body)
    assert first.status_code == 200
    duplicate = await client.post(f"/api/v1/kbs/{kb_id}/sources", json=body)
    assert duplicate.status_code == 422
    too_often = await client.put(
        f"/api/v1/kbs/{kb_id}/sources/{first.json()['source']['source_id']}",
        json={"interval_minutes": 0},
    )
    assert too_often.status_code == 422
    bad_interval = await client.post(
        f"/api/v1/kbs/{kb_id}/sources",
        json={"repo": "o/r2", "glob": "*.md", "interval_minutes": 99999},
    )
    assert bad_interval.status_code == 422


async def test_scheduled_sync_respects_due_and_interval(app_client):
    app, client, state = app_client
    kb_id = (await client.post("/api/v1/kbs", json={"name": "定时"})).json()["id"]
    state.files = {"a.md": ("s1", "傅里叶".encode())}
    added = (
        await client.post(
            f"/api/v1/kbs/{kb_id}/sources",
            json={"repo": "o/r", "glob": "*.md", "sync_now": False, "interval_minutes": 1},
        )
    ).json()
    service = app.state.github_sources
    # 从未同步过 → due；同步一次后 1 分钟内不再 due
    assert await service.sync_all_due() == 1
    assert await service.sync_all_due() == 0

    # 关掉自动同步后永远不 due
    await service.update_source(kb_id, added["source"]["source_id"], auto_sync=False)
    assert await service.sync_all_due() == 0
