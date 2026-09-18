"""/health 端点与根路径就绪探测。"""

import nnnu


async def test_health_returns_version_and_memory(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "online"
    assert data["version"] == nnnu.__version__
    assert data["python"]
    assert data["platform"]
    # psutil 缺失时 memory 为 None，存在时带 rss_mb
    assert data["memory"] is None or "rss_mb" in data["memory"]
    assert data["uptime_s"] >= 0


async def test_root_probe(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json()["version"] == nnnu.__version__


async def test_unknown_route_returns_404(client):
    resp = await client.get("/api/v1/nope")
    assert resp.status_code == 404
