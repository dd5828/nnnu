"""沙箱服务（§11.2）：子进程执行 + 资源限制 + 进程内配额。

设计要点（对照参考仓库 services/sandbox/{service,quota}.py 思路自研）：
- 服务门面持有配额与限制参数，工具层只跟 spec.py 的 ExecRequest/ExecResult 打交道；
- 超时/输出超限时杀整棵进程树（Windows taskkill /T，POSIX 进程组）；
- 工作目录路径归一化后必须落在 workspace 内（防目录穿越，fail-closed）；
- 环境变量白名单：凭据/代理一律不进沙箱。
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from nnnu.runtime import home
from nnnu.services.sandbox.spec import (
    DEFAULT_TIMEOUT_S,
    MAX_CONCURRENT,
    MAX_PER_MINUTE,
    MAX_TIMEOUT_S,
    OUTPUT_LIMIT_BYTES,
    ExecRequest,
    ExecResult,
)

logger = logging.getLogger(__name__)

# 白名单：系统运行必需；凭据/代理变量一律剔除（§11.2 环境变量白名单）
ENV_WHITELIST = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONIOENCODING", "PYTHONUTF8")
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
RUNS_DIR_NAME = ".sandbox-runs"  # 脚本/命令暂存目录（workspace 内隐藏目录）


class SandboxError(Exception):
    """沙箱层错误（路径非法/配额超限等），工具层转 ToolResult(ok=False)。"""


def _workspace_root() -> Path:
    return home.get_data_root() / "user" / "workspace"


def _resolve_cwd(cwd_relative: str) -> Path:
    """归一化相对路径并锁定在 workspace 内；越界抛 SandboxError（fail-closed）。"""
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / (cwd_relative or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise SandboxError(f"工作目录越界：{cwd_relative!r} 不在 workspace 内") from None
    return candidate


def _build_env(allow_network: bool, extra: dict[str, str]) -> dict[str, str]:
    """白名单环境：系统变量 + 显式附加；代理变量按网络开关处理，凭据不进。"""
    env: dict[str, str] = {}
    for key in ENV_WHITELIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if allow_network:
        for key in PROXY_VARS:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
    for key, value in extra.items():
        if value is not None:
            env[key] = value
    return env


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """杀整棵进程树：Windows 走 taskkill /T，POSIX 走进程组。"""
    try:
        if os.name == "nt":
            await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            killpg = getattr(os, "killpg", None)  # POSIX 专属，Windows 类型桩无此属性
            getpgid = getattr(os, "getpgid", None)
            if killpg is not None and getpgid is not None:
                killpg(getpgid(process.pid), 9)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class SandboxService:
    """进程内单例：并发/速率配额 + 两种执行（Python 代码 / shell 命令）。"""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._recent_runs: list[float] = []

    def _check_rate(self) -> None:
        cutoff = time.monotonic() - 60.0
        while self._recent_runs and self._recent_runs[0] < cutoff:
            self._recent_runs.pop(0)
        if len(self._recent_runs) >= MAX_PER_MINUTE:
            raise SandboxError(f"沙箱执行频率超限（{MAX_PER_MINUTE} 次/分钟），稍后再试")
        self._recent_runs.append(time.monotonic())

    async def run(self, request: ExecRequest) -> ExecResult:
        if request.code is None and request.command is None:
            return ExecResult(ok=False, error="code 与 command 至少提供一个")
        if not 0 < request.timeout_s <= MAX_TIMEOUT_S:
            request.timeout_s = DEFAULT_TIMEOUT_S
        try:
            cwd = _resolve_cwd(request.cwd_relative)
        except SandboxError as exc:
            return ExecResult(ok=False, error=str(exc))
        try:
            self._check_rate()
        except SandboxError as exc:
            return ExecResult(ok=False, error=str(exc))

        async with self._semaphore:
            return await self._execute(request, cwd)

    async def _execute(self, request: ExecRequest, cwd: Path) -> ExecResult:
        started = time.monotonic()
        runs_dir = cwd / RUNS_DIR_NAME
        runs_dir.mkdir(parents=True, exist_ok=True)
        env = _build_env(request.allow_network, request.env)

        if request.code is not None:
            script = runs_dir / f"run_{int(started * 1000)}_{os.getpid()}.py"
            script.write_text(request.code, encoding="utf-8")
            program, arguments = sys.executable, ["-u", str(script)]
        else:
            program, arguments = request.command or "", []
            if os.name == "nt":
                program = os.environ.get("COMSPEC") or "cmd.exe"
                arguments = ["/c", request.command or ""]
            else:
                program, arguments = "/bin/sh", ["-c", request.command or ""]

        try:
            process = await asyncio.create_subprocess_exec(
                program,
                *arguments,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            return ExecResult(
                ok=False, error=f"启动失败: {exc}", duration_s=time.monotonic() - started
            )

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        total = 0
        truncated = False
        timed_out = False

        async def pump(stream: asyncio.StreamReader | None, parts: list[bytes]) -> None:
            nonlocal total, truncated
            if stream is None:
                return
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    return
                if total + len(chunk) > OUTPUT_LIMIT_BYTES:
                    truncated = True
                    # 输出超限：立刻杀进程（process.wait() 随即返回），不再读取
                    await _kill_tree(process)
                    return
                total += len(chunk)
                parts.append(chunk)

        async def collect() -> int | None:
            """等待进程退出 + 并泵两个流；超时由 wait_for 抛出。"""
            pumps = [
                asyncio.create_task(pump(process.stdout, stdout_parts)),
                asyncio.create_task(pump(process.stderr, stderr_parts)),
            ]
            try:
                await asyncio.wait_for(process.wait(), timeout=request.timeout_s)
                for task in pumps:
                    await task
            finally:
                for task in pumps:
                    task.cancel()
            return process.returncode

        try:
            exit_code = await collect()
        except asyncio.TimeoutError:
            timed_out = True
            exit_code = None
        finally:
            if process.returncode is None:
                await _kill_tree(process)

        duration = time.monotonic() - started
        stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
        if timed_out:
            return ExecResult(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                exit_code=None,
                error=f"执行超时（{request.timeout_s:g}s），进程已终止",
                duration_s=duration,
                truncated=True,
            )
        if truncated:
            return ExecResult(
                ok=True,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                error=None,
                duration_s=duration,
                truncated=True,
            )
        ok = exit_code == 0
        return ExecResult(
            ok=ok,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            error=None if ok else f"退出码 {exit_code}",
            duration_s=duration,
        )


_sandbox_service: SandboxService | None = None


def get_sandbox_service() -> SandboxService:
    global _sandbox_service
    if _sandbox_service is None:
        _sandbox_service = SandboxService()
    return _sandbox_service


def reset_sandbox_service() -> None:
    """测试用：丢弃单例（配额状态清零）。"""
    global _sandbox_service
    _sandbox_service = None
