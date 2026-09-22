"""沙箱与基础工具（§11.2 / §7.2）：超时、输出上限、目录穿越、环境白名单、文件工具。"""

import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.sandbox.service import SandboxError, get_sandbox_service, reset_sandbox_service
from nnnu.services.sandbox.spec import ExecRequest
from nnnu.tools.builtin.code_execution import CodeExecutionTool
from nnnu.tools.builtin.exec_tool import ExecTool
from nnnu.tools.builtin.file_tools import (
    ListWorkspaceTool,
    ReadWorkspaceFileTool,
    WriteWorkspaceFileTool,
)


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
