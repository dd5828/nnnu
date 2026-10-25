"""跑一趟子代理咨询（§7.12）：起无头 CLI、把它的输出逐帧转出来、结束它。

形状照沙箱的 `collect()`（两路 pump 并发 + 等进程退出 + 收尾杀树），不照 render 的
`render()`——那里把 stderr 并进 stdout 了，我们这边 stdout 是**逐行 JSON**，混进
stderr 就是一片坏行。

四个容易踩的点，都在下面收口：

1. **读行不用 `readline()`**：StreamReader 默认 limit 是 64KiB，`tool_result` 那种长行
   一超就抛 `ValueError`，pump 直接死掉而进程还活着，回合静默卡到超时。这里改成按块
   `read()` 自己切行，顺带给单行长度封顶（超了丢这一行，不丢整趟）。
2. **prompt 走 stdin**，argv 只放我们自己的旗标（理由见 claude_code.build_argv）。
3. **杀进程先发状态、再杀树、杀完再收 pump**：pump 要等管道 EOF 才返回，先收 pump
   会白等一个活着的进程。杀那一步套 `asyncio.shield`——本函数常在被取消的回合任务里
   收尾，直接 await 会在杀到一半时被再次取消，留下孤儿 CLI。
4. **超时/取消都要把进程收干净**（`await process.wait()`），不然事件循环一关就冒
   "unclosed transport" 警告，进程也可能真的赖着不走。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable, Callable

from nnnu.core.proc import kill_tree
from nnnu.services.agents.spec import AgentConnection
from nnnu.subagents.claude_code import (
    ClaudeCodeParser,
    ConsultResult,
    Frame,
)
from nnnu.subagents.claude_code import build_argv as build_claude_argv
from nnnu.subagents.process import ProgramNotFoundError, resolve_command

#: 单次咨询的默认超时（编码任务动辄几分钟；上限见 MAX_TIMEOUT_S 钳制）
DEFAULT_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 1800.0
#: 杀进程的宽限：杀树命令本身也不该把回合拖住
KILL_GRACE_S = 3.0
#: 读取块大小与单行上限（见模块头第 1 条）
READ_CHUNK = 65536
MAX_LINE_BYTES = 4 * 1024 * 1024
#: 失败时附在结果里的 stderr 尾巴
STDERR_TAIL_CHARS = 2000

Emitter = Callable[[Frame], Awaitable[None]]


async def run_consult(
    connection: AgentConnection,
    task: str,
    *,
    resume: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    emit: Emitter | None = None,
) -> ConsultResult:
    """起一次无头咨询并等它结束（结果里带终局正文、CLI 会话 id 与结束原因）。

    `emit` 是活动流出口：null 表示没有人看（比如后台的兜底调用），此时只跑不报。
    """
    started = time.monotonic()
    result = ConsultResult()
    limit = _clamp_timeout(timeout_s)
    try:
        program, args = resolve_command(build_claude_argv(connection, resume=resume))
    except ProgramNotFoundError as exc:
        result.stop_reason = "unavailable"
        result.error = str(exc)
        result.duration_s = time.monotonic() - started
        return result

    try:
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            cwd=connection.cwd or None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 进程组自成一户，POSIX 的树杀才打得到子孙（见 core.proc）
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        result.stop_reason = "unavailable"
        result.error = f"启动失败：{exc}"
        result.duration_s = time.monotonic() - started
        return result

    parser = ClaudeCodeParser()
    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
    pumps = [
        asyncio.create_task(_pump(process.stdout, "stdout", queue)),
        asyncio.create_task(_pump(process.stderr, "stderr", queue)),
    ]
    watchdog = asyncio.create_task(_watch(process, pumps, queue))
    timed_out = False
    try:
        await _send_prompt(process, task, result)
        try:
            await asyncio.wait_for(_consume(queue, parser, emit, result), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            result.stop_reason = "timeout"
            result.error = f"子代理超过 {limit:g} 秒没有结束，已中止"
    except asyncio.CancelledError:
        result.stop_reason = result.stop_reason or "cancelled"
        result.error = result.error or "已取消"
        raise
    finally:
        stopped = process.returncode is None
        if stopped:
            # 先说话再动手：这一刻前端还能看到「正在结束子代理」，杀掉之后就没机会了
            await _emit(
                emit,
                Frame(block_id=_next_id(result), kind="status", text=_stop_line(timed_out)),
            )
        await _shutdown(process, pumps, watchdog, emit)
        if stopped and result.stop_reason in ("", "success"):
            result.stop_reason = "timeout" if timed_out else "cancelled"
        for frame in parser.close():
            await _emit(emit, frame)

    result.session_id = parser.session_id
    result.stale_session = parser.stale_session
    result.duration_s = time.monotonic() - started
    if not timed_out and not result.error:
        result.ok = parser.ok and process.returncode in (0, None)
        if not result.ok:
            result.error = _failure_reason(parser, process.returncode, result.stderr_tail)
    if result.stop_reason in ("", "success"):
        # 没跑成又没有终局事件（进程自己崩了/被外部杀了）：别自称 success
        result.stop_reason = parser.stop_reason or ("success" if result.ok else "error")
    # 终局正文优先用 result 事件的 result 字段，没有就拼所有正文块（被杀掉的那一趟
    # 也把已经说出来的话留下：部分结论对主模型仍然有用）
    result.output = (parser.result_text or parser.full_text).strip()
    return result


# ---- 内部 ----


def _clamp_timeout(value: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if seconds <= 0:
        return DEFAULT_TIMEOUT_S
    return min(seconds, MAX_TIMEOUT_S)


def _next_id(result: ConsultResult) -> str:
    result.seq += 1
    return f"runner-{result.seq}"


async def _send_prompt(
    process: asyncio.subprocess.Process, task: str, result: ConsultResult
) -> None:
    """把任务写进 stdin 后关掉它（CLI 读到 EOF 就知道正文完了）。

    子进程当场退出（参数不认、程序坏了）时写 stdin 会撞断管，这不是致命错：
    下面还要读它的 stdout/stderr 才知道到底怎么了，所以只记一条状态。
    """
    stream = process.stdin
    if stream is None:
        return
    try:
        stream.write(task.encode("utf-8"))
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        result.notes.append(f"任务没能写进子代理的标准输入（{exc}）")
    finally:
        with contextlib.suppress(Exception):
            stream.close()


async def _pump(stream: asyncio.StreamReader | None, source: str, queue: asyncio.Queue) -> None:
    """按块读、自己切行（模块头第 1 条）：单行超上限就丢掉这一行，继续读后面的。"""
    if stream is None:
        return
    buffer = b""
    dropped = 0
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        buffer += chunk
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            line, buffer = buffer[:index], buffer[index + 1 :]
            queue.put_nowait((source, line.decode("utf-8", errors="replace")))
        if len(buffer) > MAX_LINE_BYTES:
            dropped += 1
            buffer = b""
            queue.put_nowait(
                (source, f"（一行输出超过 {MAX_LINE_BYTES // 1024 // 1024}MB，已丢弃）")
            )
    if buffer.strip():
        queue.put_nowait((source, buffer.decode("utf-8", errors="replace")))
    if dropped:
        queue.put_nowait((source, f"（本次共丢弃 {dropped} 行超长输出）"))


async def _watch(
    process: asyncio.subprocess.Process,
    pumps: list[asyncio.Task],
    queue: asyncio.Queue,
) -> None:
    """等进程退出 + 两路 pump 都到 EOF，再投 None 哨兵（主循环收到它就知道结束了）。"""
    await asyncio.gather(process.wait(), *pumps, return_exceptions=True)
    queue.put_nowait(None)


async def _consume(
    queue: asyncio.Queue,
    parser: ClaudeCodeParser,
    emit: Emitter | None,
    result: ConsultResult,
) -> None:
    while True:
        item = await queue.get()
        if item is None:
            return
        source, line = item
        if source == "stderr":
            result.stderr_tail = _tail(result.stderr_tail + line + "\n")
            if line.strip():
                await _emit(
                    emit,
                    Frame(block_id=_next_id(result), kind="status", text=f"[stderr] {line}"),
                )
            continue
        frames = parser.feed(line)
        for frame in frames:
            result.frame_count += 1
            await _emit(emit, frame)


async def _emit(emit: Emitter | None, frame: Frame) -> None:
    if emit is None:
        return
    try:
        await emit(frame)
    except Exception:  # 活动流是锦上添花，发不出去也不能让这趟咨询失败
        pass


async def _shutdown(
    process: asyncio.subprocess.Process,
    pumps: list[asyncio.Task],
    watchdog: asyncio.Task,
    emit: Emitter | None,
) -> None:
    """收尾：要么它已经结束（只等收尸），要么现在就杀掉整棵树（模块头第 3、4 条）。"""
    watchdog.cancel()
    if process.returncode is None:
        # shield：本函数可能正跑在一个已被取消的任务里，裸 await 会在杀到一半时被再取消
        with contextlib.suppress(BaseException):
            await asyncio.shield(asyncio.wait_for(kill_tree(process), KILL_GRACE_S))
        with contextlib.suppress(BaseException):
            process.kill()  # 兜底：树杀失败（比如 taskkill 不在）时至少杀掉父进程
    # 收尸 + 让两路 pump 读到 EOF 自然结束（管道关掉，transport 才会被释放）。
    # 用 asyncio.wait 而不是 wait_for(shield(...))：后者的超时会摘掉 shield 的完成回调，
    # 收尸任务随后带着 CancelledError 无人认领，日志里凭空多一条 ERROR（实测）；
    # asyncio.wait 只管等，不会去动这几个任务，取消与否都由下面自己决定。
    reaper = asyncio.ensure_future(asyncio.gather(process.wait(), *pumps, return_exceptions=True))
    with contextlib.suppress(BaseException):
        await asyncio.wait({reaper}, timeout=KILL_GRACE_S)
    if not reaper.done():
        # 进程已经杀过了，这里只是不让任务悬着；watchdog 的哨兵也就没人要了
        reaper.cancel()
        reaper.add_done_callback(_discard)
    with contextlib.suppress(BaseException):
        await asyncio.gather(watchdog, return_exceptions=True)


def _discard(future: asyncio.Future) -> None:
    """丢弃任务时顺手取走它的异常：没人取的话 asyncio 会按 ERROR 记一条「never retrieved」。"""
    if not future.cancelled():
        future.exception()


def _stop_line(timed_out: bool) -> str:
    return "子代理超过时限，正在结束它…" if timed_out else "正在结束子代理…"


def _failure_reason(parser: ClaudeCodeParser, returncode: int | None, stderr_tail: str) -> str:
    """失败要说清楚是哪种失败：CLI 自己报的错 > 退出码 > stderr 尾巴。"""
    if parser.result_text and not parser.ok:
        return parser.result_text.strip()[:400]
    if returncode not in (0, None):
        tail = " ".join(stderr_tail.split())[:300]
        return f"子代理退出码 {returncode}" + (f"：{tail}" if tail else "")
    if parser.stop_reason and parser.stop_reason != "success":
        return f"子代理结束状态：{parser.stop_reason}"
    return "子代理没有给出结论"


def _tail(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    return text if len(text) <= limit else text[-limit:]
