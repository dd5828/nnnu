"""子代理进程的两件基础事：把程序解析成真能跑的东西、跑一次 `--version` 看它活不活。

流式长跑在 `runner.py`（刀二），这里只放**探测与咨询共用**的解析与一次性探活。
共用是刻意的：两边各写一套解析，就会出现「界面上探测得到、咨询时找不到程序」这种
最难解释的不一致（同 cli_apps 的 `is_installed` 以「程序真能跑」为准的理由）。

Windows 有两层坑，都在 `resolve_command` 里收口：

- `create_subprocess_exec("claude", …)` 起不来 npm 的 `.cmd` shim——CreateProcess
  不做扩展名关联，必须先解析成带后缀的绝对路径；
- 解析到 `.cmd`/`.bat` 还得包一层 `COMSPEC /c`，而 cmd.exe 会**重新解析**命令行里的
  `& | > ^ %`。所以调用方绝不能把用户/模型写的文本拼进 argv——prompt 一律走 stdin
  （见 runner 的注释）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: 探测版本的超时：--version 该是毫秒级的事，8 秒还没有输出说明这程序在等人/在挂
PROBE_TIMEOUT_S = 8.0

#: Windows 上必须经 cmd.exe 才能起的后缀
_SHELL_SUFFIXES = (".cmd", ".bat")


class ProgramNotFoundError(RuntimeError):
    """程序解析不到（没装、PATH 上没有、路径写错）。消息直接给用户看。"""


def resolve_command(argv: Sequence[str]) -> tuple[str, list[str]]:
    """argv → create_subprocess_exec 能直接吃的 `(program, args)`。

    规则写死三条，别再在别处写第二套：

    1. `argv[0]` 本身就是存在的文件（绝对/相对路径）→ 原样用，不查 PATH
       （E2E 的假 CLI 就是 `[<解释器绝对路径>, <脚本绝对路径>]`，靠这条走）；
    2. 否则按纯名字查 PATH——复用 cli_apps 的 `which_program`（整本 PATH 索引，
       Windows 认 PATHEXT，且不接受"服务启动目录当 PATH 前缀"这种 which 行为）；
    3. Windows 上解析结果带 `.cmd`/`.bat` → 包 `(COMSPEC, /c, 程序, *args)`。
    """
    if not argv:
        raise ProgramNotFoundError("启动命令是空的")
    head, rest = str(argv[0]), [str(item) for item in argv[1:]]
    if head and os.path.isfile(head):
        program = head
    else:
        from nnnu.services.cli_apps.install import which_program

        found = which_program(head)
        if not found:
            raise ProgramNotFoundError(
                f"找不到程序 {head!r}：确认它装了、且在服务的 PATH 上"
                "（服务进程的 PATH 与你的终端可能不一样，必要时在连接里写程序全路径）"
            )
        program = found
    if os.name == "nt" and program.lower().endswith(_SHELL_SUFFIXES):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return comspec, ["/c", program, *rest]
    return program, rest


async def probe_version(
    argv: Sequence[str], *, timeout_s: float = PROBE_TIMEOUT_S
) -> tuple[bool, str]:
    """跑一次 `<程序> --version`：返回 `(可用吗, 版本或原因)`。

    判据是**真的跑起来且 0 退出**，不是「PATH 里有没有这个文件」：本机就躺着一个
    这样的例子——npm 全局的 claude 三件套都在，原生二进制却没装上，任何参数都只打
    一行 "native binary not installed"。以文件存在为准的话，界面会显示「可用」，
    然后每次咨询都失败。
    """
    try:
        program, args = resolve_command(argv)
    except ProgramNotFoundError as exc:
        return False, str(exc)
    try:
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return False, f"启动失败：{exc}"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill(process)
        # 杀完必须收尸（await wait）：光 kill 不 wait，进程对象与管道留给 GC，
        # 事件循环一关就冒 "unclosed transport" 的 ResourceWarning
        with contextlib.suppress(Exception):
            await process.wait()
        return False, f"探测超时（>{timeout_s:g}s 没有响应）"
    text = (stdout or b"").decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        reason = text or (stderr or b"").decode("utf-8", errors="replace").strip()
        # 退出码非 0 也可能有话说（坏装就属于这种）：把它原样带出去，别只报「失败」
        return False, f"退出码 {process.returncode}：{_one_line(reason) or '无输出'}"
    return True, _one_line(text)


def _one_line(text: str, limit: int = 200) -> str:
    """版本号/报错都压成一行：这些文本要进界面与审计，不能带换行。"""
    return " ".join(text.split())[:limit]


def _kill(process: asyncio.subprocess.Process) -> None:
    """超时兜底：直接 kill，不看结果（探测进程通常没有子进程）。"""
    try:
        if process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
