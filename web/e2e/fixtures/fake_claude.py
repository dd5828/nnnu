#!/usr/bin/env python3
"""E2E 假 Claude Code（§7.12）：顶替真的 `claude -p --output-format stream-json …`。

为什么要有个假的：子代理这条链要验的恰恰是「真起进程 → 逐行读它的 stream-json →
超时/取消时把整棵树杀干净」，而真 CLI 装没装、装得好不好全看跑测的机器（开发机上就
躺着一个坏装：npm 全局包在，原生二进制没装上）。把外部程序换成一个我们能编排的脚本，
这些路径才每次都跑得到。

模式按**收到的任务正文**里的记号选（记号写在 `chat_scenarios.yaml` 的 task 里，界面上
看到的是任务描述，脚本看到的是 `E2E:XXX`）；不给记号就是默认那趟完整流程：

- 默认 HAPPY：`system/init` → `stream_event` 增量 → `assistant` 完整块（带 tool_use）→
  `user` 的 tool_result → `result`。中间把收到的 **argv、cwd 与 stdin** 回声成状态行——
  用例断言「旗标对、prompt 走的确实是 stdin」全靠这几行；
- `E2E:PLAIN`：老版本兜底——**一个 stream_event 都不发**，只有 assistant 完整块；顺带混
  一行坏 JSON、往 stderr 写两行（非 JSON 行与 stderr 都得变成状态行，不能让这趟失败）；
- `E2E:FAIL`：先说半句结论再以非 0 退出码收场（验「失败了也要把已经说出来的话带回来」）；
- `E2E:SLEEP`：自己再起一个子进程，然后一起睡（验超时树杀：父与孙都得死）。

`--version`（连接页的探测）只打印版本号退 0，并且**不读 stdin**：探测时 stdin 是继承来的
终端，读它会当场挂住。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: 假 CLI 报的会话 id（真实 CLI 在 `--resume` 下会沿用同一个，这里也固定住）
SESSION_ID = "e2e-claude-session"
#: 假 CLI 报的模型名（`--model` 传没传到，看的是 argv 回声那一行）
MODEL = "e2e-model"

#: 树杀验证的子进程要睡多久：父被杀之后它要是还活着，用例才来得及发现
TREE_CHILD_SLEEP = 120.0
#: SLEEP 模式的父进程睡多久（一定要超过用例给的 `timeout_s`）
KEEP_AWAKE_S = 120.0
#: 树杀验证用的 pid 文件路径（由 start-backend.mjs 通过环境变量给）
TREE_PID_ENV = "FAKE_CLAUDE_TREE_PID"

#: HAPPY 那趟的正文（增量流与终局 result 用同一份，前端覆盖写之后只该有一行）
HAPPY_TEXT = (
    "我先看一眼工程结构，再把快排写进 e2e-quick-sort.py：原地分区的递归实现，"
    "平均 O(n log n)，顺手把基准元素取中间那个避免已经有序时退化。"
)
#: PLAIN 那趟的正文（老版本只有整条消息）
PLAIN_TEXT = "老版本这趟没有逐块增量：只有一条完整的 assistant 消息，正文在这里。"
#: FAIL 那趟的半句结论
FAIL_TEXT = "我先试着读工程目录，还没读完就出事了。"
#: SLEEP 那趟的半句正文
SLEEP_TEXT = "我先看看工程结构，别急……"


# ---- 输出 ----


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _event(payload: dict) -> None:
    _out(json.dumps(payload, ensure_ascii=False))


def _noise(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _echo(argv: list[str], task: str) -> None:
    """把收到的命令行、工作目录与 stdin 回声出去（非 JSON 行 → 状态行）。

    `--output-format stream-json` 有没有传、prompt 是不是走的 stdin、cwd 是不是连接里
    那个目录，用例都从这几行里读。
    """
    _out("[argv] " + " ".join(argv))
    _out(f"[cwd] {os.getcwd()}")
    _out("[stdin] " + " ".join(task.split())[:120])
    _out("[resume] " + (_flag(argv, "--resume") or "none"))


def _flag(argv: list[str], name: str) -> str:
    """取 `--flag value` 里的值（命令行里没有就空串）。"""
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def _assistant_text(text: str, message_id: str = "msg_e2e_plain") -> None:
    """一条只带正文的完整 assistant 消息（一个 stream_event 都没有）。"""
    _event(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "id": message_id,
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


# ---- 各模式 ----


def run_happy(argv: list[str], task: str) -> int:
    _event(
        {
            "type": "system",
            "subtype": "init",
            "session_id": SESSION_ID,
            "model": MODEL,
            "cwd": os.getcwd(),
        }
    )
    _echo(argv, task)
    # 一段真的逐块增量：界面上要看得见「在打字」，用例也才有窗口断言运行中的脉动
    _stream_text(HAPPY_TEXT, message_id="msg_e2e_1")
    time.sleep(0.4)
    # 同一条消息的完整块（真实 CLI 两个都发）：块身份相同 → 前端只该有一行正文
    _event(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "id": "msg_e2e_1",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": HAPPY_TEXT},
                    {
                        "type": "tool_use",
                        "id": "toolu_e2e_1",
                        "name": "Write",
                        "input": {"file_path": "e2e-quick-sort.py", "content": "def qsort(a): ..."},
                    },
                ],
            },
        }
    )
    time.sleep(0.4)
    _event(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_e2e_1",
                        "content": [{"type": "text", "text": "已写入 e2e-quick-sort.py（18 行）"}],
                    }
                ],
            },
        }
    )
    time.sleep(0.3)
    _event(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": HAPPY_TEXT,
            "session_id": SESSION_ID,
            "duration_ms": 1800,
        }
    )
    return 0


def _stream_text(text: str, *, message_id: str, chunks: int = 5, pause: float = 0.2) -> None:
    """把正文切成几段逐块推（每段之间停一下，界面上才是一行行长出来的）。"""
    _event(
        {
            "type": "stream_event",
            "session_id": SESSION_ID,
            "event": {"type": "message_start", "message": {"id": message_id}},
        }
    )
    _event(
        {
            "type": "stream_event",
            "session_id": SESSION_ID,
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        }
    )
    for piece in _split(text, chunks):
        time.sleep(pause)
        _event(
            {
                "type": "stream_event",
                "session_id": SESSION_ID,
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            }
        )
    _event(
        {
            "type": "stream_event",
            "session_id": SESSION_ID,
            "event": {"type": "content_block_stop", "index": 0},
        }
    )


def _split(text: str, count: int) -> list[str]:
    """切成 `count` 段左右（按长度均分，不按词——E2E 只在意「来了好几帧」）。"""
    size = max(len(text) // max(count, 1), 1)
    return [text[index : index + size] for index in range(0, len(text), size)]


def run_plain(argv: list[str], task: str) -> int:
    """老版本兜底：没有 stream_event，只有完整块；坏行与 stderr 也在这趟里混一遍。"""
    _event(
        {
            "type": "system",
            "subtype": "init",
            "session_id": SESSION_ID,
            "model": MODEL,
            "cwd": os.getcwd(),
        }
    )
    # 写一半的 JSON（真实场景：进程中断/管道截断）：必须变成一条状态行，不能掀翻这趟
    _out('{"type": "assistant", "message": {')
    _echo(argv, task)
    _noise("fake claude: 这是老版本，没有 --include-partial-messages 的逐块增量")
    _assistant_text(PLAIN_TEXT)
    _noise("fake claude: 收尾警告（E2E:PLAIN）")
    _event(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": PLAIN_TEXT,
            "session_id": SESSION_ID,
        }
    )
    return 0


def run_fail(argv: list[str], task: str) -> int:
    """半句结论 + 非 0 退出码（不报 result 事件：走的是「退出码」那条失败路）。"""
    _event(
        {
            "type": "system",
            "subtype": "init",
            "session_id": SESSION_ID,
            "model": MODEL,
            "cwd": os.getcwd(),
        }
    )
    _echo(argv, task)
    _assistant_text(FAIL_TEXT)
    _noise("fake claude: 打不开工程目录，权限不足（E2E:FAIL）")
    return 1


def run_sleep(argv: list[str], task: str) -> int:
    """一直不返回：父进程与它自己起的子进程都得被超时的树杀带走。"""
    _event(
        {
            "type": "system",
            "subtype": "init",
            "session_id": SESSION_ID,
            "model": MODEL,
            "cwd": os.getcwd(),
        }
    )
    _echo(argv, task)
    _stream_text(SLEEP_TEXT, message_id="msg_e2e_sleep", chunks=3, pause=0.2)
    _noise("fake claude: 接下来一直不返回（E2E:SLEEP）")
    _spawn_tree_child()
    time.sleep(KEEP_AWAKE_S)
    return 0


def _spawn_tree_child() -> None:
    """起一个自己的子进程并把 pid 写出来，用例跑完来查它还在不在。

    树杀（Windows 的 `taskkill /T`、POSIX 的进程组）要打的就是它：只杀父进程的实现
    下，这个孙进程会继续睡 120 秒，用例当场抓得住。
    """
    raw = os.environ.get(TREE_PID_ENV, "").strip()
    if not raw:
        return  # 没给路径就只睡（手工跑这个脚本时不至于报错）
    target = Path(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({TREE_CHILD_SLEEP})"])
    target.write_text(str(child.pid), encoding="utf-8")


# ---- 入口 ----


def pick_mode(task: str) -> str:
    """任务正文 → 模式名（大写匹配；没有记号就是 HAPPY）。"""
    upper = task.upper()
    for name in ("SLEEP", "PLAIN", "FAIL"):
        if f"E2E:{name}" in upper:
            return name
    return "HAPPY"


def main() -> int:
    # 中文字节：三个流都按 utf-8 走。后端写 stdin 时编的是 utf-8，而 Windows 上解释器
    # 默认按本地代码页（cp936）解 stdin —— 不解这一下，任务正文一进来就是坏字符，
    # 回显那一步当场 UnicodeEncodeError。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", newline="\n")
    argv = sys.argv[1:]
    if "--version" in argv:
        # 探测：只报版本，不碰 stdin（那时候 stdin 是继承来的终端，读了就挂住）
        _out(f"1.2.3-e2e（假 claude，Python {sys.version.split()[0]}）")
        return 0
    task = "" if sys.stdin.isatty() else sys.stdin.read()
    mode = pick_mode(task)
    if mode == "PLAIN":
        return run_plain(argv, task)
    if mode == "FAIL":
        return run_fail(argv, task)
    if mode == "SLEEP":
        return run_sleep(argv, task)
    return run_happy(argv, task)


if __name__ == "__main__":
    sys.exit(main())
