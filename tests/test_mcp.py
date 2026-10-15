"""MCP 客户端（§7.17）：连接生命周期、工具注册与调用、进度、配置与密钥。

走真进程：`tests/fixtures/mcp_echo_server.py` 是一台手写的 stdio MCP 服务器，
连接、握手、列工具、调用全是真的（只有模型那一层是假的）。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.runtime.registry.tool_registry import get_tool_registry
from nnnu.services.mcp import tools as mcp_tools
from nnnu.services.mcp.manager import (
    MCPConnectionManager,
    _needs_authorization,
    describe_connect_failure,
)
from nnnu.services.mcp.spec import (
    MCPConfig,
    MCPServerConfig,
    load_catalog,
    load_config,
    save_config,
)
from nnnu.services.mcp.tools import wrapped_name

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


def stdio_config(**overrides) -> MCPServerConfig:
    """指向夹具服务器的 stdio 配置（默认带那个多出来的 die 工具）。"""
    data = {
        "id": "echo",
        "name": "回声服务器",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE)],
        "env": {"MCP_ECHO_EXTRA": "1"},
    }
    data.update(overrides)
    return MCPServerConfig.model_validate(data)


@pytest.fixture
async def manager(tmp_home):
    """起一个独立管理器并保证收摊（子进程不许留到下一个用例）。"""
    instance = MCPConnectionManager()
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _connect(manager: MCPConnectionManager, cfg: MCPServerConfig) -> None:
    save_config(MCPConfig(servers={cfg.id: cfg}))
    await manager.start()
    await manager.wait_ready(timeout=30)


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """等一个状态落定（连接任务发现对端死了需要一小会儿）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def test_stdio_connect_register_call_and_shutdown(manager):
    """全链路：连上 → 工具进注册表 → 调用 → 进度 → 关停注销。"""
    await _connect(manager, stdio_config())
    state = manager.state()["echo"]
    assert state["status"] == "connected"
    assert {tool["name"] for tool in state["tools"]} == {"echo", "slow_echo", "boom", "die"}

    registry = get_tool_registry()
    assert registry.get("mcp_echo_echo") is not None
    assert registry.get("mcp_echo_echo").definition.parameters["required"] == ["text"]

    outcome = await manager.call_tool("echo", "echo", {"text": "你好"}, timeout_s=10)
    assert (outcome.ok, outcome.text) == (True, "echo: 你好")

    progress: list[str] = []

    async def on_progress(current, total, message):
        progress.append(f"{current:g}/{total:g} {message}")

    outcome = await manager.call_tool(
        "echo", "slow_echo", {"text": "慢"}, timeout_s=10, on_progress=on_progress
    )
    assert outcome.ok and outcome.text == "echo: 慢"
    assert progress == ["1/2 第一步", "2/2 第二步"]

    await manager.shutdown()
    assert manager.state() == {}
    assert registry.get("mcp_echo_echo") is None


async def test_iserror_is_a_failure_not_an_empty_result(manager):
    """服务器自报失败（isError）不能当成「干完了没输出」。"""
    await _connect(manager, stdio_config())
    outcome = await manager.call_tool("echo", "boom", {}, timeout_s=10)
    assert outcome.ok is False
    assert outcome.reason == "tool_error"
    assert "boom:" in outcome.text


async def test_adapter_translates_failure_and_reports_progress(manager):
    """适配器层：进度进 progress_fn，失败翻成模型能读的话，detail 带展示名。"""
    await _connect(manager, stdio_config())
    registry = get_tool_registry()
    seen: list[str] = []

    async def progress_fn(text: str) -> None:
        seen.append(text)

    ctx = ToolContext(
        turn_id="turn-1",
        session_id="sess-1",
        metadata={"progress_fn": progress_fn},
        args={"text": "透传"},
    )
    result = await registry.get("mcp_echo_slow_echo").run(ctx)
    assert result.ok is True
    assert result.output == "echo: 透传"
    assert seen == ["第一步 (50%)", "第二步 (100%)"]
    assert result.detail["mcp_server"] == "echo"
    assert result.detail["mcp_tool"] == "slow_echo"
    assert result.detail["mcp_display"] == "mcp:echo:slow_echo"

    failed = await registry.get("mcp_echo_boom").run(
        ToolContext(turn_id="t", session_id="s", args={})
    )
    assert failed.ok is False
    assert "调用失败" in failed.output and "boom:" in failed.output

    # 没有 progress_fn 的上下文（比如别的调用方）：进度被 SDK 直接丢弃，不炸
    quiet = await registry.get("mcp_echo_slow_echo").run(
        ToolContext(turn_id="t", session_id="s", args={"text": "安静"})
    )
    assert quiet.ok is True and seen == ["第一步 (50%)", "第二步 (100%)"]


async def test_long_output_is_truncated(manager, monkeypatch):
    await _connect(manager, stdio_config())
    monkeypatch.setattr(mcp_tools, "OUTPUT_MAX_CHARS", 5)
    result = (
        await get_tool_registry()
        .get("mcp_echo_echo")
        .run(ToolContext(turn_id="t", session_id="s", args={"text": "一二三四五六七八九十"}))
    )
    assert result.ok is True
    assert result.output.startswith("echo:")
    assert "已截断" in result.output


async def test_disabled_tools_are_never_registered(manager):
    """停用是从注册表里消失，而不是挂上去再报「已停用」。"""
    await _connect(manager, stdio_config(disabled_tools=["boom", "mcp_echo_die"]))
    registry = get_tool_registry()
    assert registry.get("mcp_echo_echo") is not None
    assert registry.get("mcp_echo_boom") is None
    assert registry.get("mcp_echo_die") is None  # 包装名也认
    assert manager.state()["echo"]["tools"]  # 清单照旧完整（界面要能看见并改回来）


async def test_config_change_reconnects_and_keeps_registry_tidy(manager):
    await _connect(manager, stdio_config())
    registry = get_tool_registry()
    before = registry.get("mcp_echo_echo")
    assert before is not None

    save_config(MCPConfig(servers={"echo": stdio_config(disabled_tools=["boom"])}))
    await manager.reload()
    after = registry.get("mcp_echo_echo")
    assert after is not None and after is not before  # 指纹变了 → 真重连了
    assert registry.get("mcp_echo_boom") is None


async def test_dead_transport_unregisters_tools_and_reports(manager):
    """服务器中途死掉：工具要注销，状态要能读，别留一份「调不动」的清单。"""
    await _connect(manager, stdio_config())
    registry = get_tool_registry()
    await manager.call_tool("echo", "die", {}, timeout_s=10)
    assert await _wait_until(lambda: manager.state()["echo"]["status"] == "error")
    assert await _wait_until(lambda: registry.get("mcp_echo_echo") is None)
    assert manager.state()["echo"]["error"]

    outcome = await manager.call_tool("echo", "echo", {"text": "还有人吗"}, timeout_s=5)
    assert outcome.ok is False
    assert outcome.reason == "not_connected"


async def test_invalid_config_fails_fast_without_retrying(manager):
    """配置本身不成立（scheme 不对）就别重试：重试一万次也是同一个答案。"""
    cfg = MCPServerConfig(id="weird", transport="streamable_http", url="ftp://example.com/mcp")
    await _connect(manager, cfg)
    state = manager.state()["weird"]
    assert state["status"] == "error"
    assert "http/https" in state["error"]
    assert state["tools"] == []


async def test_bad_stdio_command_reports_the_command(manager):
    cfg = stdio_config(id="ghost", command="definitely-not-a-real-binary-xyz")
    await _connect(manager, cfg)
    state = manager.state()["ghost"]
    assert state["status"] == "error"
    assert "definitely-not-a-real-binary-xyz" in state["error"]


async def test_probe_is_one_off_and_bounded(manager):
    """探测自己开关连接：不碰活连接，失败也必须在时限内回话。"""
    await _connect(manager, stdio_config())
    async with asyncio.timeout(15):
        ok = await manager.probe(stdio_config(id="probe-target"))
        dead = await manager.probe(
            MCPServerConfig(id="dead", transport="streamable_http", url="http://127.0.0.1:9/mcp")
        )
    assert ok["ok"] is True
    assert {tool["name"] for tool in ok["tools"]} >= {"echo", "boom"}
    assert {tool["wrapped"] for tool in ok["tools"]} >= {"mcp_probe-target_echo"}
    assert dead["ok"] is False and dead["status"] == "error" and dead["error"]
    assert manager.state()["echo"]["status"] == "connected"  # 活连接没被打断


async def test_probe_accepts_unsaved_candidate_secrets(manager):
    """换密钥的流程：候选值先探通，再决定要不要落盘。"""
    await _connect(manager, stdio_config())
    probe = await manager.probe(
        stdio_config(id="candidate", env={"MCP_ECHO_EXTRA": "1", "TOKEN": "${secret:token}"}),
        override={"token": "还没落盘的候选值"},
    )
    assert probe["ok"] is True


# ---- 名字规则 ----


def test_wrapped_name_shape_and_truncation():
    assert wrapped_name("echo", "read_file") == "mcp_echo_read_file"
    assert wrapped_name("my-server", "search/issues") == "mcp_my-server_search_issues"

    long_a = wrapped_name("server-" + "a" * 40, "tool-" + "b" * 60)
    long_b = wrapped_name("server-" + "a" * 40, "tool-" + "b" * 59 + "c")
    assert len(long_a) <= 64 and len(long_b) <= 64
    assert long_a != long_b  # 砍尾巴会撞名，哈希不会
    assert long_a.isascii() and all(char.isalnum() or char in "_-" for char in long_a)


def test_server_id_rejects_traversal_and_oddities():
    from pydantic import ValidationError

    for bad in ("../evil", "Bad Name", "UPPER", "", "中文", "a" * 49):
        with pytest.raises(ValidationError):
            MCPServerConfig(id=bad)
    assert MCPServerConfig(id="a-1-ok").id == "a-1-ok"
    assert MCPServerConfig(id="echo").resolved_transport() == ""  # 没有 command/url


def test_transport_inference():
    assert MCPServerConfig(id="s", command="python").resolved_transport() == "stdio"
    assert MCPServerConfig(id="s", url="https://x/mcp").resolved_transport() == "streamable_http"
    assert MCPServerConfig(id="s", url="https://x/sse").resolved_transport() == "sse"
    assert MCPServerConfig(id="s", url="https://x/sse/").resolved_transport() == "sse"
    assert (
        MCPServerConfig(id="s", transport="sse", url="https://x/events").resolved_transport()
        == "sse"
    )


# ---- 配置与密钥 ----


def test_config_roundtrip_and_corrupt_file(tmp_home):
    cfg = stdio_config(headers={"Authorization": "${secret:token}"})
    save_config(MCPConfig(servers={"echo": cfg}))
    loaded = load_config()
    assert loaded.servers["echo"].connection_signature() == cfg.connection_signature()
    assert "${secret:token}" in (tmp_home / "data" / "user" / "mcp" / "servers.json").read_text(
        encoding="utf-8"
    )

    from nnnu.services.mcp.spec import config_path

    config_path().write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_config().servers == {}  # 坏了按空处理，别拦启动


def test_secret_references_resolve_everywhere_but_never_leak(tmp_home):
    from nnnu.services.mcp.secrets import (
        configured_fields,
        delete_secrets,
        domain_for,
        resolve_references,
        resolve_url_references,
        store_secrets,
    )
    from nnnu.services.secrets.store import get_secrets_store

    store_secrets("tavily", {"api_key": "tvly-secret-123456"})
    assert configured_fields("tavily") == {"api_key"}
    assert get_secrets_store().get(domain_for("tavily"), "api_key") == "tvly-secret-123456"

    payload = {
        "headers": {"Authorization": "Bearer ${secret:api_key}"},
        "env": {"TOKEN": "${secret:api_key}"},
        "args": ["--key=${secret:api_key}", "${secret:api_key}", "普通参数"],
    }
    resolved = resolve_references("tavily", payload)
    assert resolved["headers"]["Authorization"] == "Bearer tvly-secret-123456"
    assert resolved["env"]["TOKEN"] == "tvly-secret-123456"
    assert resolved["args"] == ["--key=tvly-secret-123456", "tvly-secret-123456", "普通参数"]

    assert (
        resolve_url_references(
            "tavily", "https://mcp.tavily.com/mcp/?tavilyApiKey=${secret:api_key}"
        )
        == "https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-secret-123456"
    )
    # 未落盘的候选值优先（探测路径）
    assert resolve_references("tavily", "${secret:api_key}", override={"api_key": "候选"}) == "候选"
    # 引用查不到 → 空串（让服务器自己报鉴权失败，别把字面量发出去）
    assert resolve_references("tavily", "${secret:missing}") == ""

    delete_secrets("tavily")
    assert configured_fields("tavily") == set()


def test_config_never_stores_plaintext(tmp_home):
    """配置是会被回显、被复制、进日志的东西——明文不该出现在里面。"""
    cfg = MCPServerConfig.model_validate(
        {
            "id": "remote",
            "transport": "streamable_http",
            "url": "https://mcp.example.com/mcp?key=${secret:api_key}",
            "headers": {"Authorization": "Bearer ${secret:token}"},
        }
    )
    save_config(MCPConfig(servers={"remote": cfg}))
    blob = json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False)
    assert "${secret:token}" in blob and "${secret:api_key}" in blob


def test_materialize_fills_headers_without_touching_the_config(tmp_home):
    """请求头是纯凭据载体：整段替换掉引用，但存下来的配置一个字节都不许变。"""
    from nnnu.services.mcp.manager import MCPConnectionManager
    from nnnu.services.mcp.secrets import store_secrets

    store_secrets("remote", {"token": "ghp_abc123", "api_key": "c7-key"})

    def materialize(**extra):
        cfg = MCPServerConfig.model_validate(
            {"id": "remote", "transport": "streamable_http", "url": "https://x/mcp", **extra}
        )
        return cfg, MCPConnectionManager._materialize(cfg)

    cfg, resolved = materialize(headers={"Authorization": "Bearer ${secret:token}"})
    assert resolved.headers["Authorization"] == "Bearer ghp_abc123"
    assert cfg.headers["Authorization"] == "Bearer ${secret:token}"

    # 凭据说明点名了进哪个头：模板渲染进去
    _, templated = materialize(
        credential={
            "kind": "apikey",
            "header": "X-Api-Key",
            "value_template": "${secret:api_key}",
            "secret_names": ["api_key"],
        }
    )
    assert templated.headers["X-Api-Key"] == "c7-key"

    # 模板留空、又只有一个字段：那个字段的值直接放进去
    _, bare = materialize(credential={"kind": "apikey", "header": "X-K", "secret_names": ["token"]})
    assert bare.headers["X-K"] == "ghp_abc123"

    # 查不到的引用换成空串：字面量不该发到服务器那边去
    _, missing = materialize(headers={"Authorization": "Bearer ${secret:nope}"})
    assert missing.headers["Authorization"] == "Bearer "
    assert "${secret:" not in missing.headers["Authorization"]


def test_materialize_leaves_the_url_to_the_url_path(tmp_home):
    """URL 走自己的路：查询参数重新编码，而不是字面塞进去。"""
    from nnnu.services.mcp.manager import MCPConnectionManager
    from nnnu.services.mcp.secrets import store_secrets

    # 真密钥里有 `+` 和 `/`（base64 里常见）：直接拼进查询串会被解释成空格
    store_secrets("remote", {"api_key": "a+b/c=d"})
    cfg = MCPServerConfig.model_validate(
        {
            "id": "remote",
            "transport": "streamable_http",
            "url": "https://x/mcp?tavilyApiKey=${secret:api_key}",
        }
    )
    resolved = MCPConnectionManager._materialize(cfg)
    assert resolved.url == "https://x/mcp?tavilyApiKey=a%2Bb%2Fc%3Dd"
    assert cfg.url.endswith("${secret:api_key}")  # 原配置一个字没动


# ---- 失败信息与授权判定 ----


def test_describe_connect_failure_unwraps_and_redacts():
    inner = ConnectionError(
        "Client error '403 Forbidden' for url 'https://mcp.example.com/mcp?key=SECRET123'"
    )
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("nested", [inner])])
    text = describe_connect_failure(group)
    assert "TaskGroup" not in text
    assert "403 Forbidden" in text
    assert "SECRET123" not in text
    assert "mcp.example.com/mcp?***" in text  # 整个查询串打码，而不是挑参数挑漏

    duplicate = ExceptionGroup("g", [ValueError("同样的话"), ValueError("同样的话")])
    assert describe_connect_failure(duplicate) == "ValueError: 同样的话"


def test_needs_authorization_looks_through_groups():
    from nnnu.services.mcp.oauth import AuthorizationRequired

    required = AuthorizationRequired("notion")
    assert _needs_authorization(required) is True
    assert _needs_authorization(ExceptionGroup("g", [required])) is True
    assert _needs_authorization(ValueError("别的错")) is False


def test_oauth_storage_roundtrip(tmp_home):
    """令牌与客户端注册存在密钥库里，读取接口永不回明文。"""
    from nnnu.services.mcp.oauth import (
        OAUTH_FIELDS,
        SecretStoreTokenStorage,
        forget,
        oauth_state,
    )
    from nnnu.services.mcp.secrets import configured_fields

    storage = SecretStoreTokenStorage("notion")

    async def _roundtrip():
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        await storage.set_tokens(
            OAuthToken(access_token="at-1", token_type="Bearer", refresh_token="rt-1")
        )
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id="cid",
                redirect_uris=["http://127.0.0.1:1234/oauth/callback"],
            )
        )
        tokens = await storage.get_tokens()
        client = await storage.get_client_info()
        return tokens, client

    tokens, client = asyncio.run(_roundtrip())
    assert tokens is not None and tokens.access_token == "at-1" and tokens.refresh_token == "rt-1"
    assert client is not None and client.client_id == "cid"
    state = oauth_state("notion")
    assert state.authorized is True and state.client_registered is True
    assert set(OAUTH_FIELDS) <= configured_fields("notion")

    forget("notion")
    assert oauth_state("notion").authorized is False
    assert not (set(OAUTH_FIELDS) & configured_fields("notion"))


def test_builtin_catalog_shape(repo_catalogs):
    entries = load_catalog()
    assert len(entries) == 10
    ids = [entry["id"] for entry in entries]
    assert ids == [
        "fetch",
        "memory",
        "sequential-thinking",
        "time",
        "deepwiki",
        "context7",
        "github",
        "tavily",
        "exa",
        "playwright",
    ]
    for entry in entries:
        assert entry["transport"] in ("stdio", "sse", "streamable_http")
        assert entry["description"]["zh"] and entry["description"]["en"]
        if entry["transport"] == "stdio":
            assert entry["command"]
        else:
            assert entry["url"].startswith("https://")
    # 目录里只有引用，没有凭据：凭据字段的值要么是空的，要么写成 ${secret:...}
    blob = json.dumps(entries, ensure_ascii=False)
    assert "${secret:" in blob
    for entry in entries:
        for field in entry["fields"]:
            assert "${secret:" in field["value_template"] or field["value_template"] == ""
