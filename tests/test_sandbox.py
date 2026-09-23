"""沙箱与基础工具（§11.2 / §7.2）：超时、输出上限、目录穿越、环境白名单、文件工具。"""

import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.audit import read_audit
from nnnu.services.sandbox.service import SandboxError, get_sandbox_service, reset_sandbox_service
from nnnu.services.sandbox.spec import ExecRequest, ExecResult
from nnnu.services.settings.service import get_settings_service
from nnnu.tools.builtin.code_execution import CodeExecutionTool
from nnnu.tools.builtin.exec_tool import ExecTool
from nnnu.tools.builtin.file_tools import (
    ListWorkspaceTool,
    ReadWorkspaceFileTool,
    WriteWorkspaceFileTool,
)

PRINT_PROXY = "import os; print('proxy=', os.environ.get('http_proxy'))"


@pytest.fixture(autouse=True)
def fresh_sandbox(tmp_home):
    """每个测试独享配额状态（NNNU_HOME 由 conftest 隔离）。"""
    reset_sandbox_service()
    yield
    reset_sandbox_service()


def _ctx(**args) -> ToolContext:
    return ToolContext(turn_id="turn-test", session_id="sess-test", args=dict(args))


# ---- 沙箱服务 ----


async def test_run_code_returns_stdout(tmp_home):
    result = await get_sandbox_service().run(ExecRequest(code="print('hello sandbox')"))
    assert result.ok is True
    assert result.stdout.strip() == "hello sandbox"
    assert result.exit_code == 0


async def test_run_code_nonzero_exit(tmp_home):
    result = await get_sandbox_service().run(ExecRequest(code="raise RuntimeError('boom')"))
    assert result.ok is False
    assert "boom" in result.stderr
    assert result.exit_code != 0


async def test_timeout_kills_process(tmp_home):
    result = await get_sandbox_service().run(
        ExecRequest(code="import time; time.sleep(30)", timeout_s=1)
    )
    assert result.ok is False
    assert "超时" in (result.error or "")
    assert result.truncated is True
    assert result.duration_s < 10


async def test_output_limit_truncates_and_kills(tmp_home):
    result = await get_sandbox_service().run(
        ExecRequest(code="print('x' * (4 * 1024 * 1024))", timeout_s=30)
    )
    assert result.truncated is True
    assert len(result.stdout) <= 1.2 * 1024 * 1024


async def test_cwd_locked_to_workspace(tmp_home):
    result = await get_sandbox_service().run(
        ExecRequest(code="print(1)", cwd_relative="../../../../")
    )
    assert result.ok is False
    assert "越界" in (result.error or "")


async def test_cwd_subdir_works(tmp_home):
    result = await get_sandbox_service().run(
        ExecRequest(
            code="from pathlib import Path; Path('nested.txt').write_text('ok')", cwd_relative="sub"
        )
    )
    assert result.ok is True
    nested = tmp_home / "data" / "user" / "workspace" / "sub" / "nested.txt"
    assert nested.read_text(encoding="utf-8") == "ok"


async def test_env_whitelist_excludes_credentials(tmp_home, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-should-not-leak")
    monkeypatch.setenv("http_proxy", "http://proxy.local:8080")
    result = await get_sandbox_service().run(
        ExecRequest(
            code=(
                "import os\n"
                "print('key=', os.environ.get('DEEPSEEK_API_KEY'))\n"
                "print('proxy=', os.environ.get('http_proxy'))"
            )
        )
    )
    assert "key= None" in result.stdout
    assert "proxy= None" in result.stdout


async def test_shell_command(tmp_home):
    result = await get_sandbox_service().run(ExecRequest(command="echo shell-ok"))
    assert result.ok is True
    assert "shell-ok" in result.stdout


async def test_rate_limit(tmp_home):
    import time

    service = get_sandbox_service()
    service._recent_runs = [time.monotonic()] * 30  # 窗口内 30 次，下一发即超限
    result = await service.run(ExecRequest(code="print(1)"))
    assert result.ok is False
    assert "频率超限" in (result.error or "")


# ---- 工具层 ----


async def test_code_execution_tool(tmp_home):
    result = await CodeExecutionTool().run(_ctx(code="print('42')"))
    assert result.ok is True
    assert "42" in result.output


async def test_code_execution_tool_timeout(tmp_home):
    result = await CodeExecutionTool().run(
        _ctx(code="import time; time.sleep(30)", timeout_seconds=1)
    )
    assert result.ok is False
    assert "超时" in result.output


async def test_exec_tool_returns_output(tmp_home):
    result = await ExecTool().run(_ctx(command="echo from-shell"))
    assert result.ok is True
    assert "from-shell" in result.output


async def test_workspace_file_roundtrip(tmp_home):
    write = await WriteWorkspaceFileTool().run(_ctx(path="notes/hello.txt", content="你好，工作区"))
    assert write.ok is True
    read = await ReadWorkspaceFileTool().run(_ctx(path="notes/hello.txt"))
    assert read.ok is True
    assert read.output == "你好，工作区"
    listing = await ListWorkspaceTool().run(_ctx(path="notes"))
    assert listing.ok is True
    assert "hello.txt" in listing.output


async def test_file_tools_reject_traversal(tmp_home):
    outside = tmp_home / "data" / "secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("secret", encoding="utf-8")
    read = await ReadWorkspaceFileTool().run(_ctx(path="../../secret.txt"))
    assert read.ok is False
    assert "越界" in read.output
    write = await WriteWorkspaceFileTool().run(_ctx(path="../../evil.txt", content="x"))
    assert write.ok is False


async def test_read_missing_file(tmp_home):
    result = await ReadWorkspaceFileTool().run(_ctx(path="nope.txt"))
    assert result.ok is False
    assert "不存在" in result.output


# ---- 联网总开关与执行审计（§11.2 / §11.6） ----


async def test_network_off_by_default(tmp_home, monkeypatch):
    """§11.2 网络默认关闭：模型请求了也不放行（代理变量进不去）。"""
    monkeypatch.setenv("http_proxy", "http://proxy.local:8080")
    result = await get_sandbox_service().run(ExecRequest(code=PRINT_PROXY, allow_network=True))
    assert "proxy= None" in result.stdout


async def test_network_opens_with_setting(tmp_home, monkeypatch):
    """§11.2 用户可开：设置里开「沙箱允许联网」后，请求联网的代码才拿到代理变量。"""
    monkeypatch.setenv("http_proxy", "http://proxy.local:8080")
    get_settings_service().save_area("chat", {"sandbox_network": True})
    result = await get_sandbox_service().run(ExecRequest(code=PRINT_PROXY, allow_network=True))
    assert "proxy= http://proxy.local:8080" in result.stdout


async def test_network_setting_does_not_force_network(tmp_home, monkeypatch):
    """开了总开关也不强推：没请求联网的代码依旧拿不到代理变量（调用方只能收紧）。"""
    monkeypatch.setenv("http_proxy", "http://proxy.local:8080")
    get_settings_service().save_area("chat", {"sandbox_network": True})
    result = await get_sandbox_service().run(ExecRequest(code=PRINT_PROXY))
    assert "proxy= None" in result.stdout


async def test_code_execution_tool_follows_network_setting(tmp_home, monkeypatch):
    """工具层接线：allow_network 参数经服务层总开关放行。"""
    monkeypatch.setenv("http_proxy", "http://proxy.local:8080")
    get_settings_service().save_area("chat", {"sandbox_network": True})
    result = await CodeExecutionTool().run(_ctx(code=PRINT_PROXY, allow_network=True))
    assert result.ok is True
    assert "proxy= http://proxy.local:8080" in result.output


async def test_exec_tool_requests_network(tmp_home, monkeypatch):
    """exec 没有联网参数：请求恒为"要"，放行与否全看服务层读到的总开关（§11.2）。"""
    captured: dict[str, bool] = {}

    async def fake_run(request):
        captured["allow_network"] = request.allow_network
        return ExecResult(ok=True, exit_code=0, duration_s=0.0)

    monkeypatch.setattr(get_sandbox_service(), "run", fake_run)
    await ExecTool().run(_ctx(command="echo hi"))
    assert captured["allow_network"] is True


async def test_sandbox_execution_is_audited(tmp_home):
    """§11.6：每次沙箱执行落一条审计，且能追到回合与会话。"""
    result = await CodeExecutionTool().run(_ctx(code="print('audited')"))
    assert result.ok is True
    entries = [e for e in read_audit() if e.get("action") == "sandbox_exec"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "code"
    assert entry["ok"] is True
    assert entry["exit_code"] == 0
    assert "audited" in entry["target"]
    assert entry["turn_id"] == "turn-test"
    assert entry["session_id"] == "sess-test"


async def test_denied_sandbox_run_is_audited(tmp_home):
    """越界被拒也算一次执行尝试，审计同样留痕。"""
    await get_sandbox_service().run(ExecRequest(code="print(1)", cwd_relative="../../"))
    entries = [e for e in read_audit() if e.get("action") == "sandbox_exec"]
    assert len(entries) == 1
    assert entries[0]["ok"] is False
    assert "越界" in (entries[0]["error"] or "")
