"""子代理连接（§7.12 刀一）：配置形态、服务层校验、REST、探测、schema v16。

测试里的「子代理程序」照 cli_apps 的办法用当前解释器（`sys.executable`）：它一定
存在、跨平台、还能按需写退出码与耗时，比造一个假 .exe 稳。探测要的三条路径
（退 0 / 退非 0 / 根本找不到）都靠它凑出来。
"""

import json
import os
import sys

import pytest

from nnnu.core.ids import stable_id
from nnnu.services.agents.service import (
    AgentConnectionInvalid,
    AgentConnectionNotFound,
    AgentService,
)
from nnnu.services.agents.spec import (
    AGENT_KINDS,
    DEFAULT_COMMANDS,
    AgentConnection,
    command_allowed,
    config_path,
)
from nnnu.subagents.process import ProgramNotFoundError, probe_version, resolve_command

GHOST_PROGRAM = "nnnu-no-such-agent-program-xyz"

#: 一条真能跑、且会按需打印内容的「子代理」
PY_OK = [sys.executable, "-c", "print('fake-claude 1.2.3')"]
#: 退出码非 0 且只在 stderr 有话说（坏装的形状：有输出、但不是版本号）
PY_FAIL = [
    sys.executable,
    "-c",
    "import sys; sys.stderr.write('native binary missing'); sys.exit(3)",
]


@pytest.fixture
def allow_custom(monkeypatch):
    """放开自定义启动命令（默认是关的，见 spec.command_allowed）。"""
    monkeypatch.setenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", "1")


# ---- 形态（spec） ----


def test_command_allowed_reads_env(monkeypatch):
    monkeypatch.delenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", raising=False)
    assert command_allowed() is False
    for value in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", value)
        assert command_allowed() is True
    for value in ("0", "no", ""):
        monkeypatch.setenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", value)
        assert command_allowed() is False


def test_connection_argv_head_prefers_custom():
    default = AgentConnection(id="ag-1", kind="claude_code")
    assert default.argv_head() == list(DEFAULT_COMMANDS["claude_code"])
    custom = AgentConnection(id="ag-2", kind="claude_code", command=["C:/x/claude.exe"])
    assert custom.argv_head() == ["C:/x/claude.exe"]


def test_connection_rejects_unknown_kind_and_mode():
    with pytest.raises(Exception):
        AgentConnection(id="ag-1", kind="not-a-kind")
    with pytest.raises(Exception):
        AgentConnection(id="ag-1", kind="claude_code", permission_mode="whatever")


def test_supported_kinds_is_subset_of_all():
    # 本批只打通 claude_code（其余能建能探、咨询时如实拒绝）
    from nnnu.services.agents.spec import SUPPORTED_KINDS

    assert set(SUPPORTED_KINDS) <= set(AGENT_KINDS)
    assert SUPPORTED_KINDS == ("claude_code",)


def test_stable_id_is_deterministic_and_shaped():
    first = stable_id("imp", "claude_code::abc-123")
    second = stable_id("imp", "claude_code::abc-123")
    assert first == second
    assert first.startswith("imp-") and len(first) == len("imp-") + 8
    assert stable_id("imp", "claude_code::other") != first
    with pytest.raises(ValueError):
        stable_id("nope", "x")


# ---- 配置落盘 ----


def test_save_then_load_roundtrip(tmp_home):
    from nnnu.services.agents.spec import AgentConfig, load_config, save_config

    save_config(AgentConfig())
    assert load_config().connections == {}
    assert json.loads(config_path().read_text(encoding="utf-8")) == {"connections": {}}


def test_corrupt_config_reads_as_empty(tmp_home):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    from nnnu.services.agents.spec import load_config

    assert load_config().connections == {}


# ---- 服务层 ----


async def test_create_requires_acknowledgement(tmp_home):
    service = AgentService()
    with pytest.raises(AgentConnectionInvalid, match="acknowledged"):
        await service.create(kind="claude_code")  # 默认 acknowledged=False


async def test_create_persists_and_lists(tmp_home):
    service = AgentService()
    created = await service.create(
        kind="claude_code", display_name="本机 Claude", acknowledged=True
    )
    assert created.id.startswith("ag-")
    # 磁盘上真有一份（重启后还在），且 cwd 默认落在 workspace
    stored = AgentService().get(created.id)
    assert stored is not None and stored.label() == "本机 Claude"
    assert stored.cwd.endswith("workspace")
    assert [item.id for item in service.list()] == [created.id]
    assert service.count() == 1


async def test_create_rejects_bad_cwd(tmp_home):
    service = AgentService()
    with pytest.raises(AgentConnectionInvalid, match="绝对路径"):
        await service.create(kind="claude_code", cwd="relative/dir", acknowledged=True)
    with pytest.raises(AgentConnectionInvalid, match="不存在"):
        await service.create(kind="claude_code", cwd=str(tmp_home / "nope"), acknowledged=True)


async def test_create_rejects_unknown_kind(tmp_home):
    service = AgentService()
    with pytest.raises(AgentConnectionInvalid, match="未知子代理类型"):
        await service.create(kind="claude_cli", acknowledged=True)


async def test_custom_command_gated_by_env(tmp_home, monkeypatch):
    monkeypatch.delenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", raising=False)
    service = AgentService()
    with pytest.raises(AgentConnectionInvalid, match="自定义启动命令"):
        await service.create(kind="claude_code", command=PY_OK, acknowledged=True)
    assert service.count() == 0  # 拒了就不该留下半条

    monkeypatch.setenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", "1")
    created = await service.create(kind="claude_code", command=PY_OK, acknowledged=True)
    assert created.argv_head() == PY_OK


async def test_resolve_prefers_selected_then_single(tmp_home):
    service = AgentService()
    assert service.resolve(None) is None  # 一条都没有，挑不出来
    only = await service.create(kind="claude_code", display_name="唯一", acknowledged=True)
    assert service.resolve(None).id == only.id  # 只有一条 → 自动选

    second = await service.create(kind="codex", acknowledged=True)
    assert service.resolve(None) is None  # 两条又都没指定 → 不替用户猜
    assert service.resolve(second.id).id == second.id
    assert service.resolve(None, session_selected=only.id).id == only.id
    # 指定了一个不存在的 id：退回会话选中项，而不是崩
    assert service.resolve("ag-deadbeef", session_selected=only.id).id == only.id


async def test_delete_and_missing(tmp_home):
    service = AgentService()
    created = await service.create(kind="claude_code", acknowledged=True)
    await service.delete(created.id)
    assert service.count() == 0
    with pytest.raises(AgentConnectionNotFound):
        await service.delete(created.id)


async def test_unset_service_raises():
    from nnnu.services.agents.service import get_agent_service, set_agent_service

    set_agent_service(None)
    with pytest.raises(RuntimeError, match="未装配"):
        get_agent_service()


# ---- 探测（真跑一次） ----


@pytest.mark.skipif(os.name != "nt", reason="COMSPEC 包装是 Windows 专有分支")
def test_resolve_command_wraps_cmd_shims(tmp_path):
    """npm 装的 .cmd shim 必须经 cmd.exe 起：CreateProcess 不做扩展名关联。

    这也是「prompt 一律走 stdin」的理由所在——cmd.exe 会重新解析命令行里的
    `& | > ^ %`，任务正文进了 argv 就会被截断或展开。
    """
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\r\necho claude 1.2.3\r\n", encoding="utf-8")
    program, args = resolve_command([str(shim), "-p", "--output-format", "stream-json"])
    assert program == os.environ.get("COMSPEC") or "cmd.exe"
    assert args == ["/c", str(shim), "-p", "--output-format", "stream-json"]


async def test_resolve_command_uses_existing_file(tmp_home):
    # 第一条规则：argv[0] 是存在的文件就原样用（E2E 假 CLI 靠它）
    program, args = resolve_command(PY_OK)
    assert program == sys.executable and args == PY_OK[1:]


async def test_probe_missing_program(tmp_home):
    with pytest.raises(ProgramNotFoundError):
        resolve_command([GHOST_PROGRAM])
    ok, detail = await probe_version([GHOST_PROGRAM, "--version"])
    assert ok is False and "找不到程序" in detail


async def test_probe_success_and_failure(tmp_home):
    ok, detail = await probe_version([*PY_OK, "--version"])
    assert ok is True and "fake-claude 1.2.3" in detail

    ok, detail = await probe_version([*PY_FAIL, "--version"])
    assert ok is False and "退出码 3" in detail and "native binary missing" in detail


async def test_probe_timeout_kills_process(tmp_home):
    ok, detail = await probe_version(
        [sys.executable, "-c", "import time; time.sleep(30)", "--version"], timeout_s=0.5
    )
    assert ok is False and "超时" in detail


# ---- REST ----


async def test_api_list_empty(client):
    response = await client.get("/api/v1/agents")
    assert response.status_code == 200
    data = response.json()
    assert data["connections"] == []
    assert [row["kind"] for row in data["kinds"]] == list(AGENT_KINDS)
    assert data["custom_command_allowed"] is False


async def test_api_create_needs_acknowledgement(client):
    response = await client.post("/api/v1/agents", json={"kind": "claude_code"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "agent_connection_invalid"


async def test_api_create_delete_roundtrip(client):
    created = await client.post(
        "/api/v1/agents",
        json={"kind": "claude_code", "display_name": "本机 Claude", "acknowledged": True},
    )
    assert created.status_code == 200
    connection = created.json()["connection"]
    assert connection["label"] == "本机 Claude"
    assert connection["supported"] is True
    assert connection["argv"] == list(DEFAULT_COMMANDS["claude_code"])

    listed = (await client.get("/api/v1/agents")).json()["connections"]
    assert [row["id"] for row in listed] == [connection["id"]]

    deleted = await client.delete(f"/api/v1/agents/{connection['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/v1/agents")).json()["connections"] == []
    assert (await client.delete(f"/api/v1/agents/{connection['id']}")).status_code == 404


async def test_api_probe_runs_the_program(client, allow_custom):
    created = await client.post(
        "/api/v1/agents",
        json={"kind": "claude_code", "command": PY_OK, "acknowledged": True},
    )
    connection_id = created.json()["connection"]["id"]

    ok = (await client.post(f"/api/v1/agents/{connection_id}/probe")).json()["probe"]
    assert ok["available"] is True and "fake-claude 1.2.3" in ok["detail"]
    assert ok["argv"][-1] == "--version"  # 探的是版本，不是随便跑一下

    broken = await client.post(
        "/api/v1/agents",
        json={"kind": "claude_code", "command": PY_FAIL, "acknowledged": True},
    )
    broken_id = broken.json()["connection"]["id"]
    probe = (await client.post(f"/api/v1/agents/{broken_id}/probe")).json()["probe"]
    assert probe["available"] is False and "退出码 3" in probe["detail"]

    assert (await client.post("/api/v1/agents/ag-deadbeef/probe")).status_code == 404


async def test_api_probe_marks_unsupported_kind(client, allow_custom):
    """程序装了但本批没接这条链路：探测结果要如实说明，别让用户以为条条能跑。"""
    created = await client.post(
        "/api/v1/agents",
        json={"kind": "codex", "command": PY_OK, "acknowledged": True},
    )
    connection_id = created.json()["connection"]["id"]
    probe = (await client.post(f"/api/v1/agents/{connection_id}/probe")).json()["probe"]
    assert probe["available"] is True
    assert probe["supported"] is False
    assert "尚未接入" in probe["detail"]
