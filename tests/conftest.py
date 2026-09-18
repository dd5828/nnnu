"""共享夹具：临时 home 目录 + httpx 测试客户端（跑 lifespan）。"""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """把 NNNU_HOME 指向临时目录，隔离真实 data/。"""
    monkeypatch.setenv("NNNU_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
async def client(tmp_home):
    from nnnu.api.main import create_app

    app = create_app()
    # httpx ASGITransport 不自动跑 lifespan，手动进入 lifespan 上下文
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
