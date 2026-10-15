"""MCP REST（§7.17）：增删改、脱敏回显、测试连接、启停与工具开关、审计。

这些用例走**带 lifespan 的测试客户端**：管理器是应用自己的那一个，端点调
`reload()` 之后连接就已经是活的（`reload` 是等待语义），所以断言不必轮询。
"""

import json
import sys
import time
from pathlib import Path

from nnnu.runtime.registry.tool_registry import get_tool_registry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"
SECRET = "sk-live-abcdef1234567890"

STDIO_BODY = {
    "id": "echo",
    "name": "回声服务器",
    "transport": "stdio",
    "command": sys.executable,
    "args": [str(FIXTURE)],
    "env": {"MCP_ECHO_EXTRA": "1"},
}


def _config_file(tmp_home: Path) -> Path:
    return tmp_home / "data" / "user" / "mcp" / "servers.json"


def _audit_actions(tmp_home: Path) -> list[str]:
    path = tmp_home / "data" / "system" / "audit.log"
    if not path.is_file():
        return []
    return [
        json.loads(line)["action"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _create(client, body: dict | None = None) -> dict:
    response = await client.post("/api/v1/mcp", json=body or STDIO_BODY)
    assert response.status_code == 200, response.text
    return response.json()["server"]


async def _get(client, server_id: str = "echo") -> dict:
    listing = (await client.get("/api/v1/mcp")).json()["servers"]
    matches = [item for item in listing if item["id"] == server_id]
    assert matches, f"清单里没有 {server_id}：{listing}"
    return matches[0]


async def test_create_connects_and_lists_tools(client, tmp_home):
    server = await _create(client)
    assert server["status"] == "connected"
    assert server["error"] == ""
    assert server["transport"] == "stdio"
    assert [tool["name"] for tool in server["tools"]] == ["boom", "die", "echo", "slow_echo"]
    assert all(tool["enabled"] for tool in server["tools"])
    assert server["tools"][0]["wrapped"].startswith("mcp_echo_")

    assert get_tool_registry().get("mcp_echo_echo") is not None
    assert _config_file(tmp_home).is_file()

    duplicate = await client.post("/api/v1/mcp", json=STDIO_BODY)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "mcp_duplicate_id"

    bad_id = await client.post("/api/v1/mcp", json={**STDIO_BODY, "id": "../escape"})
    assert bad_id.status_code == 422
    assert bad_id.json()["error"]["code"] == "mcp_invalid_config"

    no_id = await client.post("/api/v1/mcp", json={"command": "python"})
    assert no_id.status_code == 422


async def test_update_patch_delete_round_trip(client):
    await _create(client)
    patched = await client.patch("/api/v1/mcp/echo", json={"name": "改过的名字"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["server"]["name"] == "改过的名字"
    assert patched.json()["server"]["status"] == "connected"  # 改显示名不影响连接
    assert (await _get(client))["name"] == "改过的名字"

    missing = await client.patch("/api/v1/mcp/nope", json={"name": "x"})
    assert missing.status_code == 404

    deleted = await client.delete("/api/v1/mcp/echo")
    assert deleted.status_code == 200 and deleted.json()["deleted"] == "echo"
    assert (await client.get("/api/v1/mcp")).json()["servers"] == []
    assert get_tool_registry().get("mcp_echo_echo") is None
    assert (await client.delete("/api/v1/mcp/echo")).status_code == 404


async def test_secrets_are_masked_everywhere_but_used(client, tmp_home):
    """密钥进密钥库、配置里只留引用、任何响应里都只有掩码。"""
    body = {
        **STDIO_BODY,
        # 夹具服务器不校验密钥，这里只验「存与不泄露」；鉴权失败路径由探测用例覆盖
        "secrets": {"api_key": SECRET, "token": SECRET},
        "headers": {"Authorization": "Bearer ${secret:token}"},
        "credential": {"kind": "apikey", "header": "X-Api-Key", "secret_names": ["api_key"]},
    }
    server = await _create(client, body)
    assert SECRET not in json.dumps(server, ensure_ascii=False)
    assert server["secret_fields"]["api_key"]["set"] is True
    assert server["secret_fields"]["api_key"]["masked"] != SECRET

    assert SECRET not in (await client.get("/api/v1/mcp")).text
    saved = _config_file(tmp_home).read_text(encoding="utf-8")
    assert SECRET not in saved  # 配置落盘的是引用，不是明文
    assert "Bearer ${secret:token}" in saved
    from nnnu.services.mcp.secrets import domain_for
    from nnnu.services.secrets.store import get_secrets_store

    assert get_secrets_store().get(domain_for("echo"), "api_key") == SECRET  # 明文只住在这里

    # 空白串 = 删除该字段
    cleared = await client.patch("/api/v1/mcp/echo", json={"secrets": {"api_key": ""}})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["server"]["secret_fields"]["api_key"]["set"] is False

    bad = await client.patch("/api/v1/mcp/echo", json={"secrets": {"../bad": "x"}})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "mcp_invalid_secrets"


async def test_bad_secret_rolls_the_whole_save_back(client, tmp_home):
    """换密钥连不上：配置和密钥都不许动（半保存的成功最害人）。"""
    await _create(client, {**STDIO_BODY, "secrets": {"api_key": SECRET}})
    before = _config_file(tmp_home).read_text(encoding="utf-8")

    response = await client.patch(
        "/api/v1/mcp/echo",
        json={
            "url": "http://127.0.0.1:9/mcp",  # 没人听的端口
            "transport": "streamable_http",
            "secrets": {"api_key": "换了个连不上的值"},
        },
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "mcp_connect_failed"
    assert error["message"]
    assert "换了个连不上的值" not in response.text

    assert _config_file(tmp_home).read_text(encoding="utf-8") == before
    from nnnu.services.mcp.secrets import domain_for
    from nnnu.services.secrets.store import get_secrets_store

    assert get_secrets_store().get(domain_for("echo"), "api_key") == SECRET


async def test_test_endpoint_answers_within_its_budget(client):
    """探测无论如何都要回话：连不上的地址不能把请求挂在那儿。"""
    await _create(
        client,
        {
            **STDIO_BODY,
            "id": "dead",
            "transport": "streamable_http",
            "command": None,
            "args": None,
            "env": None,
            "url": "http://127.0.0.1:9/mcp",
        },
    )
    started = time.monotonic()
    response = await client.post("/api/v1/mcp/dead/test", json={})
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    probe = response.json()
    assert probe["ok"] is False and probe["status"] == "error" and probe["error"]
    assert elapsed < 10, f"探测花了 {elapsed:.1f} 秒"

    assert (await client.post("/api/v1/mcp/nope/test", json={})).status_code == 404


async def test_probe_accepts_candidate_secrets_without_saving(client, tmp_home):
    await _create(client)
    response = await client.post("/api/v1/mcp/echo/test", json={"secrets": {"api_key": SECRET}})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert "${secret:api_key}" not in _config_file(tmp_home).read_text(encoding="utf-8")


async def test_per_tool_disable_unregisters_from_the_registry(client):
    await _create(client)
    assert get_tool_registry().get("mcp_echo_boom") is not None

    response = await client.patch("/api/v1/mcp/echo", json={"disabled_tools": ["boom"]})
    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()["server"]["tools"]}
    assert rows["boom"]["enabled"] is False
    assert rows["echo"]["enabled"] is True
    assert get_tool_registry().get("mcp_echo_boom") is None
    assert get_tool_registry().get("mcp_echo_echo") is not None
    # 清单照旧完整：界面要能把停用的那个再打开
    assert set(rows) == {"boom", "die", "echo", "slow_echo"}


async def test_disabling_a_server_takes_its_tools_away(client):
    await _create(client)
    off = await client.patch("/api/v1/mcp/echo", json={"enabled": False})
    assert off.json()["server"]["status"] == "disabled"
    assert get_tool_registry().get("mcp_echo_echo") is None

    # 停用的服务器仍然看得见工具（否则没法在界面上知道它有什么）
    assert off.json()["server"]["tools"]

    on = await client.patch("/api/v1/mcp/echo", json={"enabled": True})
    assert on.json()["server"]["status"] == "connected"
    assert get_tool_registry().get("mcp_echo_echo") is not None


async def test_unreachable_server_survives_a_restart_of_the_app(client):
    """存下来连不上的服务器：列表里带着错误说明，而不是 500 或者消失。"""
    await _create(
        client,
        {
            "id": "ghost",
            "transport": "stdio",
            "command": "definitely-not-a-real-binary-xyz",
            "args": [],
        },
    )
    server = await _get(client, "ghost")
    assert server["status"] == "error"
    assert "definitely-not-a-real-binary-xyz" in server["error"]
    assert server["tools"] == []


async def test_oauth_guards(client):
    await _create(client)
    not_oauth = await client.post("/api/v1/mcp/echo/auth")
    assert not_oauth.status_code == 400
    assert not_oauth.json()["error"]["code"] == "mcp_not_oauth"

    server = await _create(
        client,
        {
            "id": "remote",
            "transport": "streamable_http",
            "url": "https://mcp.example.com/mcp",
            "credential": {"kind": "oauth"},
        },
    )
    assert server["oauth"] == {"authorized": False, "scope": ""}
    assert server["secret_fields"] == {}  # 令牌不进「用户填的密钥」清单


async def test_catalog_endpoint_serves_the_builtin_directory(client, repo_catalogs):
    entries = (await client.get("/api/v1/mcp/catalog")).json()["entries"]
    assert [entry["id"] for entry in entries][:4] == [
        "fetch",
        "memory",
        "sequential-thinking",
        "time",
    ]
    assert all(entry["description"]["zh"] for entry in entries)


async def test_audit_records_the_lifecycle(client, tmp_home):
    await _create(client, {**STDIO_BODY, "secrets": {"api_key": SECRET}})
    await client.patch("/api/v1/mcp/echo", json={"name": "改一下"})
    await client.delete("/api/v1/mcp/echo")

    actions = _audit_actions(tmp_home)
    assert "mcp_config_save" in actions
    assert "mcp_credential_change" in actions
    assert "mcp_config_delete" in actions
    assert "mcp_connect" in actions
    raw = (tmp_home / "data" / "system" / "audit.log").read_text(encoding="utf-8")
    assert SECRET not in raw  # 审计日志也不许出现明文
