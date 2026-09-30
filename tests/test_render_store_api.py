"""渲染产物存储、假渲染器与下载端点（§7.8）：落盘/防穿越/白名单/失败注入。"""

import pytest

from nnnu.services.render.artifacts import RenderStore, RenderStoreError
from nnnu.services.render.service import (
    MockManimRenderService,
    build_render_service,
)

GOOD_CODE = (
    "from manim import *\n\n\nclass Demo(Scene):\n    def construct(self):\n        self.wait(1)\n"
)
FAIL_CODE = GOOD_CODE.replace("self.wait(1)", "self.wait(1)  # NNNU_MOCK_FAIL")


def _store(tmp_home) -> RenderStore:
    return RenderStore(tmp_home / "data")


# ---- 存储层 ----


def test_store_roundtrip(tmp_home):
    store = _store(tmp_home)
    render_id = store.new_render_id()
    store.save(render_id, b"bytes", filename="animation.mp4", kind="video", attempts=2)
    found = store.get(render_id)
    assert found is not None
    path, meta = found
    assert path.read_bytes() == b"bytes"
    assert meta.filename == "animation.mp4"
    assert meta.attempts == 2
    assert meta.url() == f"/api/v1/renders/{render_id}"


def test_store_rejects_bad_ids(tmp_home):
    store = _store(tmp_home)
    for bad in ("rnd-00112233/../x", "rnd-../../etc", "sess-00112233", "rnd-AB12CD34"):
        assert store.get(bad) is None
        with pytest.raises(RenderStoreError):
            store.ensure_dir(bad)


def test_store_rejects_suffix_outside_allowlist(tmp_home):
    store = _store(tmp_home)
    render_id = store.new_render_id()
    for name in ("page.html", "figure.svg", "scene.py"):
        with pytest.raises(RenderStoreError):
            store.save(render_id, b"x", filename=name, kind="image")


def test_store_sanitizes_traversal_filename(tmp_home):
    store = _store(tmp_home)
    render_id = store.new_render_id()
    meta = store.save(render_id, b"x", filename="../../evil.mp4", kind="video")
    assert meta.filename == "evil.mp4"
    path, _ = store.get(render_id)  # type: ignore[misc]
    assert path.parent == (store.root / render_id).resolve()


def test_store_get_missing_returns_none(tmp_home):
    assert _store(tmp_home).get("rnd-00112233") is None


# ---- 假渲染器 ----


async def _collect():
    lines: list[str] = []

    async def sink(line: str) -> None:
        lines.append(line)

    return lines, sink


async def test_mock_render_success(tmp_home):
    store = _store(tmp_home)
    service = MockManimRenderService(store)
    lines, sink = await _collect()
    render_id = store.new_render_id()
    result = await service.render(render_id, code=GOOD_CODE, quality="low", on_log=sink)
    assert result.ok and result.meta is not None
    assert result.meta.filename == "animation.mp4"
    assert store.get(render_id) is not None
    assert any("假渲染器" in line for line in lines)


async def test_mock_render_injected_failure(tmp_home):
    store = _store(tmp_home)
    service = MockManimRenderService(store)
    render_id = store.new_render_id()
    result = await service.render(render_id, code=FAIL_CODE)
    assert not result.ok and "NNNU_MOCK_FAIL" in result.error
    assert store.get(render_id) is None  # 失败不落 meta


def test_build_render_service_env_switch(tmp_home, monkeypatch):
    store = _store(tmp_home)
    monkeypatch.delenv("NNNU_MANIM_MOCK", raising=False)
    from nnnu.services.render.service import ManimRenderService

    assert isinstance(build_render_service(store), ManimRenderService)
    monkeypatch.setenv("NNNU_MANIM_MOCK", "1")
    assert isinstance(build_render_service(store), MockManimRenderService)


# ---- 下载端点 ----


async def test_render_endpoint_inline_and_download(client, tmp_home):
    store = _store(tmp_home)
    render_id = store.new_render_id()
    store.save(render_id, b"video-bytes", filename="animation.mp4", kind="video")

    response = await client.get(f"/api/v1/renders/{render_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content == b"video-bytes"

    download = await client.get(f"/api/v1/renders/{render_id}?download=1")
    assert download.headers["content-disposition"].startswith("attachment")


async def test_render_endpoint_missing_is_404(client):
    for bad in ("rnd-00112233", "rnd-XXYYZZ99", "not-an-id"):
        response = await client.get(f"/api/v1/renders/{bad}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
