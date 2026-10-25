"""子代理咨询（§7.12 刀二）：协议解析、运行器、工具与挂载门。

测试里的「子代理 CLI」都用当前解释器跑一段写出来的脚本（`sys.executable -u <脚本>`）：
它跨平台、能精确控制退出码/输出时序，还能自己再起子进程（树杀那条要验的东西）。
连接配置里的启动命令本来只有 `NNNU_AGENTS_ALLOW_CUSTOM_COMMAND=1` 才接受——这也正是
E2E 用假 CLI 的办法，这里顺带把这道门走上。

三块分开测：解析器是纯函数（假时钟掐节流）、运行器靠假 CLI 出真进程、工具走服务层。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.agents import cli_sessions
from nnnu.services.agents.spec import AgentConnection
from nnnu.subagents.claude_code import ClaudeCodeParser, Frame, build_argv
from nnnu.subagents.runner import run_consult

GHOST_PROGRAM = "nnnu-no-such-agent-program-xyz"
#: 带进 argv 断言的任务正文：它绝不能出现在命令行里（Windows 的 .cmd 会被 cmd.exe 二次解析）
TASK = "把 qsort.py 里的快排改成三路切分，并解释为什么更快"


# ---- 夹具与小工具 ----


class _Clock:
    """假时钟：节流窗口按「过了多久」断言，不靠 sleep 碰运气。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Collector:
    """把 emit 出来的帧收起来（runner 的出口、工具的活动流口）。"""

    def __init__(self) -> None:
        self.frames: list[Frame] = []

    async def __call__(self, frame: Frame) -> None:
        self.frames.append(frame)

    def texts(self) -> list[str]:
        return [frame.text for frame in self.frames]

    def find(self, prefix: str) -> Frame:
        for frame in self.frames:
            if frame.text.startswith(prefix):
                return frame
        raise AssertionError(f"没有以 {prefix!r} 开头的帧：{self.texts()}")


def _line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _connection(tmp_path: Path, args: list[str], **kwargs) -> AgentConnection:
    # `-X utf8` + `-u`：Windows 上 Python 默认拿系统代码页（GBK）编管道里的 IO，
    # 而真 CLI（Node）一律走 UTF-8、解析端也按 UTF-8 解——假 CLI 得跟真的一样，
    # 否则测出来的是「解释器默认编码」，不是协议
    return AgentConnection(
        id="ag-test",
        kind="claude_code",
        display_name="测试子代理",
        cwd=str(tmp_path),
        command=[sys.executable, "-X", "utf8", "-u", *args],
        **kwargs,
    )


async def _wait_for_file(path: Path, *, timeout: float = 6.0) -> None:
    """等假 CLI 把第一口心跳写出来（进程真起来了才开始后面的事）。"""
    for _ in range(int(timeout / 0.05)):
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"假 CLI 没起来：{path} 一直是空的")


async def _assert_frozen(path: Path, *, pause: float = 0.5) -> None:
    """心跳文件不再变化 = 进程（或它的子孙）真被杀了。"""
    before = path.read_text(encoding="utf-8")
    await asyncio.sleep(pause)
    assert path.read_text(encoding="utf-8") == before, f"{path} 还在被写：进程没死"


# ---- 解析器（纯函数） ----


def _start(parser: ClaudeCodeParser, message_id: str = "m-1") -> list[Frame]:
    return parser.feed(
        _line(
            {
                "type": "stream_event",
                "event": {"type": "message_start", "message": {"id": message_id}},
            }
        )
    )


def _open_block(parser: ClaudeCodeParser, index: int = 0) -> list[Frame]:
    return parser.feed(
        _line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            }
        )
    )


def _delta(parser: ClaudeCodeParser, text: str, index: int = 0) -> list[Frame]:
    return parser.feed(
        _line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": text},
                },
            }
        )
    )


def test_deltas_are_throttled_into_full_snapshots():
    """节流窗口内不发；发出去的一律是「这一块现在的全文」，前端只做覆盖写。"""
    clock = _Clock()
    parser = ClaudeCodeParser(clock=clock, throttle_s=0.1)
    _start(parser)
    assert _open_block(parser) == []
    assert _delta(parser, "你") == []
    clock.advance(0.05)
    assert _delta(parser, "好") == []
    clock.advance(0.2)
    frames = _delta(parser, "！")
    assert [frame.text for frame in frames] == ["你好！"]
    assert frames[0].block_id == "m-1:0"
    assert frames[0].kind == "block"


def test_block_stop_and_close_flush_full_text():
    clock = _Clock()
    parser = ClaudeCodeParser(clock=clock, throttle_s=0.1)
    _start(parser)
    _open_block(parser)
    _delta(parser, "半截话")
    clock.advance(1.0)
    closed = parser.feed(
        _line({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    )
    assert [frame.text for frame in closed] == ["半截话"]
    # 已关闭的块不再重复补帧
    assert (
        parser.feed(
            _line({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
        )
        == []
    )
    # 进程被杀时最后一块没等到 stop：close() 兜底补一帧
    _start(parser, "m-2")
    _open_block(parser)
    _delta(parser, "没说完")
    clock.advance(1.0)
    assert [frame.text for frame in parser.close()] == ["没说完"]
    assert parser.close() == []


def test_assistant_block_reuses_the_same_block_id():
    """完整消息与增量同 id：幂等覆盖，不会出现「一半增量 + 一半整块」的串台。"""
    clock = _Clock()
    parser = ClaudeCodeParser(clock=clock, throttle_s=0.0)  # 不节流，逐帧都发
    _start(parser)
    _open_block(parser)
    streamed = _delta(parser, "我来写")
    assert streamed[0].block_id == "m-1:0"
    frames = parser.feed(
        _line(
            {
                "type": "assistant",
                "message": {
                    "id": "m-1",
                    "content": [{"type": "text", "text": "我来写个快排"}],
                },
            }
        )
    )
    assert frames[0].block_id == "m-1:0"
    assert frames[0].text == "我来写个快排"


def test_assistant_without_message_id_falls_back():
    """老版本 CLI 只有完整块（没有 message_start）：id 退到消息序号，仍然稳定。"""
    parser = ClaudeCodeParser()
    frames = parser.feed(
        _line(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "没有 id 的老版本"}]},
            }
        )
    )
    assert frames[0].block_id == "msg-0:0"
    assert frames[0].text == "没有 id 的老版本"


def test_thinking_block_keeps_its_own_kind():
    parser = ClaudeCodeParser()
    _start(parser)
    parser.feed(
        _line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            }
        )
    )
    parser.feed(
        _line(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "先看目录结构"},
                },
            }
        )
    )
    frames = parser.feed(
        _line({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    )
    assert frames[0].kind == "thinking"
    assert frames[0].text == "先看目录结构"
    # 思考不进 full_text（那是结论正文的兜底）
    assert parser.full_text == ""


def test_tool_use_and_tool_result_frames():
    parser = ClaudeCodeParser()
    frames = parser.feed(
        _line(
            {
                "type": "assistant",
                "message": {
                    "id": "m-1",
                    "content": [
                        {"type": "text", "text": "先看看"},
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": "qsort.py", "content": "x" * 400, "n": 3},
                        },
                    ],
                },
            }
        )
    )
    assert [frame.kind for frame in frames] == ["block", "tool"]
    tool = frames[1]
    assert tool.block_id == "m-1:1" and tool.name == "Write"
    assert tool.args["file_path"] == "qsort.py"
    assert len(tool.args["content"]) == 160  # 单值截断：几 KB 的正文不往界面搬
    assert tool.args["n"] == "3"  # 非字符串值压成一行

    results = parser.feed(
        _line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "written"}],
                            "is_error": False,
                        },
                        {"type": "tool_result", "content": "boom", "is_error": True},
                    ]
                },
            }
        )
    )
    assert [frame.ok for frame in results] == [True, False]
    assert results[0].text == "written" and results[1].text == "boom"
    assert results[0].block_id != results[1].block_id  # 两条结果不能互相覆盖


def test_arg_keys_are_capped():
    parser = ClaudeCodeParser()
    frames = parser.feed(
        _line(
            {
                "type": "assistant",
                "message": {
                    "id": "m-1",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {f"k{i}": i for i in range(10)},
                        }
                    ],
                },
            }
        )
    )
    assert len(frames[0].args) == 6


def test_bad_and_noisy_lines_never_raise():
    parser = ClaudeCodeParser()
    assert parser.feed("") == []
    assert parser.feed("   ") == []
    bad = parser.feed("这不是 JSON，是 CLI 的一句抱怨")
    assert bad[0].kind == "status" and "抱怨" in bad[0].text
    assert parser.feed("[1, 2, 3]")[0].kind == "status"  # 合法 JSON 但不是对象
    assert parser.feed(_line({"type": "rate_limit_event"})) == []
    system = parser.feed(
        _line({"type": "system", "subtype": "init", "model": "sonnet", "cwd": "/tmp/x"})
    )
    assert "sonnet" in system[0].text and "/tmp/x" in system[0].text


def test_long_lines_are_clipped_not_dropped():
    parser = ClaudeCodeParser()
    frames = parser.feed(
        _line(
            {
                "type": "assistant",
                "message": {"id": "m-1", "content": [{"type": "text", "text": "长" * 30_000}]},
            }
        )
    )
    assert "已截断" in frames[0].text
    assert len(frames[0].text) < 30_000


def test_result_event_sets_ok_and_stop_reason():
    parser = ClaudeCodeParser()
    assert (
        parser.feed(
            _line(
                {"type": "result", "subtype": "success", "is_error": False, "result": "结论是这样"}
            )
        )
        == []
    )
    assert parser.ok is True
    assert parser.stop_reason == "success"
    assert parser.result_text == "结论是这样"
    assert parser.stale_session is False


def test_result_event_error_reports_status_frame():
    parser = ClaudeCodeParser()
    frames = parser.feed(
        _line(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "result": "跑满轮次了",
            }
        )
    )
    assert parser.ok is False
    assert parser.stop_reason == "error_max_turns"
    assert "跑满轮次了" in frames[0].text


def test_session_id_picked_from_any_event_and_stale_marker():
    parser = ClaudeCodeParser()
    parser.feed(_line({"type": "assistant", "session_id": "cli-9", "message": {}}))
    assert parser.session_id == "cli-9"
    parser.feed(
        _line(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "No such session: cli-old",
            }
        )
    )
    assert parser.stale_session is True
    assert parser.ok is False


def test_build_argv_carries_flags_not_the_task():
    plain = AgentConnection(id="ag-1", kind="claude_code")
    argv = build_argv(plain)
    assert argv[:1] == ["claude"]
    for flag in ("-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert TASK not in argv  # 任务正文只能走 stdin

    tuned = AgentConnection(
        id="ag-2", kind="claude_code", model_override="sonnet", permission_mode="plan"
    )
    argv = build_argv(tuned, resume="cli-9")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--resume") + 1] == "cli-9"


# ---- 运行器（真进程） ----

#: 一条「正常跑完」的假 CLI：增量 → 完整块 → 工具调用 → 工具结果 → 回声 argv/stdin → 终局。
#: 回声那一帧是给断言用的：prompt 走的必须确实是 stdin，命令行里只能有我们自己的旗标。
STREAM_CLI = """
import json, sys

task = sys.stdin.read()


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": "cli-sess-1",
      "model": "sonnet", "cwd": "/tmp/demo"})
emit({"type": "stream_event", "session_id": "cli-sess-1",
      "event": {"type": "message_start", "message": {"id": "m1"}}})
emit({"type": "stream_event",
      "event": {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}})
for piece in ("我来", "写个", "快排"):
    emit({"type": "stream_event",
          "event": {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": piece}}})
emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
emit({"type": "assistant", "session_id": "cli-sess-1",
      "message": {"id": "m1", "content": [
          {"type": "text", "text": "我来写个快排"},
          {"type": "tool_use", "name": "Write", "input": {"file_path": "qsort.py"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "written", "is_error": False}]}})
emit({"type": "assistant", "message": {"id": "m2", "content": [
    {"type": "text", "text": "ARGS=" + json.dumps(sys.argv[1:], ensure_ascii=False)
                             + "|STDIN=" + task}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": "快排写好了，见 qsort.py"})
"""


async def test_run_consult_happy_path(tmp_path):
    script = _script(tmp_path, "stream_cli.py", STREAM_CLI)
    collector = Collector()
    result = await run_consult(_connection(tmp_path, [script]), TASK, emit=collector)

    assert result.ok is True
    assert result.stop_reason == "success"
    assert result.output == "快排写好了，见 qsort.py"
    assert result.session_id == "cli-sess-1"
    assert result.stale_session is False
    assert result.error == "" and result.notes == []
    assert result.frame_count == len(collector.frames) > 0

    kinds = [frame.kind for frame in collector.frames]
    assert kinds[0] == "status"  # system/init 那行
    assert "tool" in kinds and "tool_result" in kinds
    # 同一块的多帧里最后一帧就是全文（增量/完整块都是覆盖写，不叠加）
    final = [frame for frame in collector.frames if frame.block_id == "m1:0"][-1]
    assert final.kind == "block" and final.text == "我来写个快排"
    tool = next(frame for frame in collector.frames if frame.kind == "tool")
    assert tool.name == "Write" and tool.args == {"file_path": "qsort.py"}

    # prompt 走 stdin，argv 只带我们自己的旗标
    echo = collector.find("ARGS=").text
    args_text, stdin_text = echo.split("|STDIN=")
    args = json.loads(args_text[len("ARGS=") :])
    assert "--output-format" in args and "stream-json" in args
    assert "--include-partial-messages" in args and "--verbose" in args
    assert TASK not in args and all(TASK not in item for item in args)
    assert stdin_text == TASK


async def test_run_consult_tolerates_bad_lines_and_stderr(tmp_path):
    body = """
import json, sys

sys.stdin.read()
sys.stdout.write("第一行就不是 JSON\\n")
sys.stdout.flush()
sys.stderr.write("警告：配置里有个没认出来的键\\n")
sys.stderr.flush()
sys.stdout.write(json.dumps({"type": "assistant", "message": {
    "id": "m1", "content": [{"type": "text", "text": "还是有结论的"}]}}) + "\\n")
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "result": "还是有结论的"}) + "\\n")
sys.stdout.flush()
"""
    script = _script(tmp_path, "noisy_cli.py", body)
    collector = Collector()
    result = await run_consult(_connection(tmp_path, [script]), TASK, emit=collector)

    assert result.ok is True
    assert result.output == "还是有结论的"
    assert any("第一行就不是 JSON" in text for text in collector.texts())
    assert any(text.startswith("[stderr]") for text in collector.texts())
    assert "没认出来的键" in result.stderr_tail


async def test_run_consult_reports_nonzero_exit(tmp_path):
    body = """
import sys

sys.stdin.read()
sys.stderr.write("native binary not installed\\n")
sys.exit(3)
"""
    script = _script(tmp_path, "broken_cli.py", body)
    collector = Collector()
    result = await run_consult(_connection(tmp_path, [script]), TASK, emit=collector)

    assert result.ok is False
    assert result.stop_reason == "error"  # 没有终局事件又没跑成，不能自称 success
    assert "退出码 3" in result.error
    assert "native binary" in result.error


async def test_run_consult_missing_program_is_unavailable(tmp_path):
    connection = AgentConnection(id="ag-x", kind="claude_code", command=[GHOST_PROGRAM])
    result = await run_consult(connection, TASK)
    assert result.ok is False
    assert result.stop_reason == "unavailable"
    assert "找不到程序" in result.error


async def test_run_consult_cli_error_result_is_failure(tmp_path):
    body = """
import json, sys

sys.stdin.read()
sys.stdout.write(json.dumps({"type": "result", "subtype": "error_max_turns",
                             "is_error": True, "result": "轮次用完了"}) + "\\n")
sys.stdout.flush()
"""
    script = _script(tmp_path, "error_cli.py", body)
    result = await run_consult(_connection(tmp_path, [script]), TASK)
    assert result.ok is False
    assert result.stop_reason == "error_max_turns"
    assert result.error == "轮次用完了"


HEARTBEAT_CLI = """
import os, sys, time, pathlib

sys.stdin.read()
path = pathlib.Path(os.environ["FAKE_CLI_HEARTBEAT"])
n = 0
while True:
    n += 1
    path.write_text(str(n), encoding="utf-8")
    time.sleep(0.1)
"""


async def test_run_consult_timeout_kills_the_process(tmp_path, monkeypatch):
    script = _script(tmp_path, "heartbeat_cli.py", HEARTBEAT_CLI)
    beat = tmp_path / "heartbeat.txt"
    monkeypatch.setenv("FAKE_CLI_HEARTBEAT", str(beat))
    collector = Collector()
    result = await run_consult(_connection(tmp_path, [script]), TASK, timeout_s=0.5, emit=collector)

    assert result.ok is False
    assert result.stop_reason == "timeout"
    assert "0.5" in result.error
    assert any("正在结束" in text for text in collector.texts())
    await _assert_frozen(beat)


TREE_CLI = """
import os, subprocess, sys, time, pathlib

subprocess.Popen([sys.executable, os.environ["FAKE_CLI_HB_SCRIPT"],
                  os.environ["FAKE_CLI_CHILD_HB"]])
sys.stdin.read()
path = pathlib.Path(os.environ["FAKE_CLI_PARENT_HB"])
n = 0
while True:
    n += 1
    path.write_text(str(n), encoding="utf-8")
    time.sleep(0.1)
"""

HB_CLI = """
import sys, time, pathlib

path = pathlib.Path(sys.argv[1])
n = 0
while True:
    n += 1
    path.write_text(str(n), encoding="utf-8")
    time.sleep(0.1)
"""


async def test_run_consult_cancel_kills_the_whole_tree(tmp_path, monkeypatch):
    """停回合 = 取消任务：子代理**和它自己起的子进程**都得死，不能留孤儿在后台跑。"""
    script = _script(tmp_path, "tree_cli.py", TREE_CLI)
    hb_script = _script(tmp_path, "hb_cli.py", HB_CLI)
    parent, child = tmp_path / "parent.txt", tmp_path / "child.txt"
    monkeypatch.setenv("FAKE_CLI_HB_SCRIPT", hb_script)
    monkeypatch.setenv("FAKE_CLI_PARENT_HB", str(parent))
    monkeypatch.setenv("FAKE_CLI_CHILD_HB", str(child))

    task = asyncio.create_task(run_consult(_connection(tmp_path, [script]), TASK, timeout_s=30))
    await _wait_for_file(parent)
    await _wait_for_file(child)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_frozen(parent)
    await _assert_frozen(child)


async def test_run_consult_drops_overlong_lines_and_keeps_going(tmp_path):
    """单行超上限只能丢那一行：丢了整趟就等于「CLI 多打印一行脏东西 = 咨询失败」。"""
    body = """
import json, sys

sys.stdin.read()
sys.stdout.write("x" * (5 * 1024 * 1024))  # 一行 5MB，且迟迟不给换行
sys.stdout.flush()
sys.stdout.write("\\n")
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "result": "超长行之后照样有结论"}) + "\\n")
sys.stdout.flush()
"""
    script = _script(tmp_path, "fat_cli.py", body)
    collector = Collector()
    result = await run_consult(_connection(tmp_path, [script]), TASK, emit=collector)

    assert result.ok is True
    assert result.output == "超长行之后照样有结论"
    assert any("已丢弃" in text for text in collector.texts())


async def test_run_consult_through_a_cmd_shim(tmp_path):
    """整条链路走一遍 `.cmd`（npm 装出来的就是这种 shim）：解析成 COMSPEC /c 之后，
    任务正文仍然经 cmd.exe 从 stdin 进得去——argv 里只留我们自己的旗标。"""
    if os.name != "nt":
        pytest.skip("cmd.exe 包装是 Windows 专有分支")
    _script(tmp_path, "fake_cli.py", STREAM_CLI)
    shim = tmp_path / "claude.cmd"
    shim.write_text(
        f'@echo off\r\n"{sys.executable}" -X utf8 -u "%~dp0fake_cli.py" %*\r\n', encoding="utf-8"
    )
    connection = AgentConnection(
        id="ag-cmd", kind="claude_code", cwd=str(tmp_path), command=[str(shim)]
    )
    collector = Collector()
    result = await run_consult(connection, TASK, emit=collector)

    assert result.ok is True
    assert result.output == "快排写好了，见 qsort.py"
    args = json.loads(collector.find("ARGS=").text[len("ARGS=") :].split("|STDIN=")[0])
    assert "--output-format" in args and TASK not in " ".join(args)
    assert collector.find("ARGS=").text.endswith(TASK)


async def test_run_consult_survives_emit_raising(tmp_path):
    """活动流是锦上添花：emit 抛异常不能让这趟咨询失败。"""
    script = _script(tmp_path, "stream_cli.py", STREAM_CLI)

    async def boom(frame: Frame) -> None:
        raise RuntimeError("前端断了")

    result = await run_consult(_connection(tmp_path, [script]), TASK, emit=boom)
    assert result.ok is True
    assert result.output == "快排写好了，见 qsort.py"


# ---- 工具与挂载门 ----


@pytest.fixture
def agent_service(tmp_home, monkeypatch):
    """临时 data 根 + 装配好的子代理服务（自定义命令这道门开给测试用）。"""
    from nnnu.services.agents.service import AgentService, set_agent_service

    monkeypatch.setenv("NNNU_AGENTS_ALLOW_CUSTOM_COMMAND", "1")
    service = AgentService()
    set_agent_service(service)
    yield service
    set_agent_service(None)


async def _make_connection(service, tmp_path, script_body: str, name: str = "cli.py", **kwargs):
    script = _script(tmp_path, name, script_body)
    return await service.create(
        kind="claude_code",
        display_name="测试子代理",
        cwd=str(tmp_path),
        command=[sys.executable, "-X", "utf8", "-u", script],
        acknowledged=True,
        **kwargs,
    )


def _tool_ctx(**overrides) -> ToolContext:
    base = {
        "turn_id": "turn-1",
        "session_id": "s1",
        "language": "zh",
        "args": {"task": TASK},
        "metadata": {},
    }
    base.update(overrides)
    return ToolContext(**base)


async def test_tool_without_any_connection_says_so(agent_service, repo_prompts):
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    result = await ConsultSubagentTool().run(_tool_ctx())
    assert result.ok is False
    assert "还没有配置子代理连接" in result.output


async def test_tool_reports_unknown_and_ambiguous_connection(agent_service, tmp_path, repo_prompts):
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    tool = ConsultSubagentTool()
    first = await _make_connection(agent_service, tmp_path, STREAM_CLI, "a.py")
    second = await _make_connection(agent_service, tmp_path, STREAM_CLI, "b.py")

    ambiguous = await tool.run(_tool_ctx())  # 两条连接、又没选中：得让模型点名
    assert ambiguous.ok is False
    assert "多条子代理连接" in ambiguous.output
    assert {first.id, second.id} <= set(ambiguous.detail["connections"])

    missing = await tool.run(_tool_ctx(args={"task": TASK, "connection": "ag-nope"}))
    assert missing.ok is False
    assert "没有叫" in missing.output and first.id in missing.output

    picked = await tool.run(_tool_ctx(args={"task": TASK, "connection": second.id}))
    assert picked.ok is True and picked.detail["connection"] == second.id


async def test_tool_falls_back_to_the_only_connection(agent_service, tmp_path, repo_prompts):
    """只有一条连接时模型把 id 写错了也照样跑它（选择链见 AgentService.resolve）。"""
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    only = await _make_connection(agent_service, tmp_path, STREAM_CLI)
    result = await ConsultSubagentTool().run(
        _tool_ctx(args={"task": TASK, "connection": "ag-typo"})
    )
    assert result.ok is True
    assert result.detail["connection"] == only.id


async def test_tool_rejects_unsupported_kind(agent_service, tmp_path, repo_prompts):
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    other = await agent_service.create(kind="codex", cwd=str(tmp_path), acknowledged=True)
    result = await ConsultSubagentTool().run(_tool_ctx(args={"task": TASK, "connection": other.id}))
    assert result.ok is False
    assert "还没接上它的咨询链路" in result.output
    assert result.detail["kind"] == "codex"


async def test_tool_needs_a_task(agent_service, repo_prompts):
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    result = await ConsultSubagentTool().run(_tool_ctx(args={"task": "  "}))
    assert result.ok is False
    assert "明确的任务描述" in result.output


async def test_tool_consult_then_resume(agent_service, tmp_path, repo_prompts):
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    connection = await _make_connection(agent_service, tmp_path, STREAM_CLI)
    collector = Collector()
    tool = ConsultSubagentTool()

    first = await tool.run(_tool_ctx(metadata={"subagent_emit": collector}, session_id="s1"))
    assert first.ok is True
    assert first.output.startswith("【子代理「测试子代理」的结论】")
    assert first.output.endswith("快排写好了，见 qsort.py")
    assert first.detail["connection"] == connection.id
    assert first.detail["agent_session_id"] == "cli-sess-1"
    assert first.detail["resumed"] is False
    assert collector.frames  # 活动流真的从工具里流出来了

    # 同一会话再问一次：该接着 CLI 那边的那段会话聊
    again = Collector()
    second = await tool.run(_tool_ctx(metadata={"subagent_emit": again}, session_id="s1"))
    assert second.detail["resumed"] is True
    assert second.output.startswith("【子代理「测试子代理」的结论（接着上次那条会话聊的）】")
    args = json.loads(again.find("ARGS=").text[len("ARGS=") :].split("|STDIN=")[0])
    assert args[args.index("--resume") + 1] == "cli-sess-1"
    assert cli_sessions.lookup("s1", connection.id) == "cli-sess-1"


STALE_CLI = """
import json, sys

sys.stdin.read()


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


if "--resume" in sys.argv:
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": "No such session: stale-1", "session_id": "cli-old"})
else:
    emit({"type": "assistant", "session_id": "cli-new",
          "message": {"id": "n1", "content": [
              {"type": "text", "text": "重开一段，结论是先排序再合并。"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "重开一段，结论是先排序再合并。", "session_id": "cli-new"})
"""


async def test_tool_retries_once_when_the_cli_session_is_gone(
    agent_service, tmp_path, repo_prompts
):
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    connection = await _make_connection(agent_service, tmp_path, STALE_CLI, "stale.py")
    await cli_sessions.remember("s2", connection.id, "stale-1")

    result = await ConsultSubagentTool().run(_tool_ctx(session_id="s2"))
    assert result.ok is True
    assert "先排序再合并" in result.output
    assert result.detail["resumed"] is False  # 重开的那一趟不该再自称「接着聊」
    assert result.detail["agent_session_id"] == "cli-new"
    assert cli_sessions.lookup("s2", connection.id) == "cli-new"


async def test_tool_uses_the_session_selected_connection(agent_service, tmp_path, repo_prompts):
    """会话粘性选中那条：模型不点名时用会话选的那条，而不是「唯一那条」。"""
    from nnnu.tools.builtin.subagent_tool import ConsultSubagentTool

    first = await _make_connection(agent_service, tmp_path, STREAM_CLI, "a.py")
    second = await _make_connection(agent_service, tmp_path, STREAM_CLI, "b.py")
    result = await ConsultSubagentTool().run(
        _tool_ctx(metadata={"agent_connection": second.id}, args={"task": TASK})
    )
    assert result.ok is True
    assert result.detail["connection"] == second.id
    assert first.id != second.id


def _chat_ctx():
    from nnnu.core.context import SessionRef, UnifiedContext
    from nnnu.services.sessions.models import Message

    return UnifiedContext(
        session=SessionRef(id="s1"),
        capability="chat",
        message=Message.new(session_id="s1", role="user", content="让子代理帮我写个快排"),
        language="zh",
    )


async def test_mount_gate_stays_shut_without_connections(agent_service, repo_prompts):
    from nnnu.capabilities._shared import mount_tools
    from nnnu.runtime.bootstrap import register_builtins

    register_builtins()
    ctx = _chat_ctx()
    mounted = await mount_tools(ctx)
    assert "consult_subagent" not in mounted.tools  # 一条连接都没有就别挂给模型
    assert "consult_subagent" not in ctx.tool_flags.context


async def test_mount_gate_opens_with_a_connection(agent_service, tmp_path, repo_prompts):
    from nnnu.capabilities._shared import mount_tools
    from nnnu.runtime.bootstrap import register_builtins

    register_builtins()
    await _make_connection(agent_service, tmp_path, STREAM_CLI)
    ctx = _chat_ctx()
    mounted = await mount_tools(ctx)
    assert "consult_subagent" in mounted.tools
    assert "consult_subagent" in ctx.tool_flags.context


async def test_mount_gate_stays_shut_when_service_is_missing(tmp_home, repo_prompts):
    """服务没装配 = 不知道有没有连接 → fail-closed（同技能/命令行应用的门）。"""
    from nnnu.capabilities._shared import _agent_gate
    from nnnu.services.agents.service import set_agent_service

    set_agent_service(None)
    assert _agent_gate() is False


def test_prompts_have_both_languages(repo_prompts):
    """工具文案两语言成对（P11 踩过：漏一边，英文界面下渲染成空串）。"""
    from nnnu.services.i18n.prompts import get_prompt_manager

    manager = get_prompt_manager()
    keys = [
        "no_connection",
        "unknown_connection",
        "pick_connection",
        "unsupported_kind",
        "empty_task",
        "header",
        "header_resumed",
        "incomplete",
        "failed",
        "no_output",
        "truncated",
    ]
    for key in keys:
        for language in ("zh", "en"):
            assert manager.has_key("agents", language, key), f"{language} 缺 {key}"
    for language in ("zh", "en"):
        assert manager.has_key("chat", language, "tool_descriptions.consult_subagent")
        assert manager.has_key("chat", language, "tool_cost_hints.consult_subagent")


# ---- 粘性读写链与回合缓冲 ----


@pytest.fixture
async def turn_manager(tmp_home):
    """真 SessionManager + 只用来测粘性那一小段的 TurnRuntimeManager。"""
    from nnnu.runtime.turn_runtime import TurnRuntimeManager
    from nnnu.services.sessions.db import Database
    from nnnu.services.sessions.schema import db_path, migrate
    from nnnu.services.sessions.service import SessionManager

    data_root = tmp_home / "data"
    migrate(data_root)
    db = Database(db_path(data_root))
    await db.connect()
    try:
        yield TurnRuntimeManager(sessions=SessionManager(db), costs=None, orchestrator=None)
    finally:
        await db.close()


async def test_agent_selection_round_trip(turn_manager):
    """请求带了就落库、没带就沿用会话里的、显式空串 = 取消（§7.12 粘性）。"""
    from nnnu.runtime.turn_runtime import TurnRequest

    sessions = turn_manager._sessions
    session = await sessions.ensure_session(None)

    explicit = TurnRequest(session_id=session.id, message="让子代理看看", agent_connection="ag-1")
    await turn_manager._apply_agent_selection(explicit, session)
    assert explicit.agent_connection == "ag-1"
    assert (await sessions.get_session(session.id)).agent_connection == "ag-1"

    # 没带这个字段的回合（regenerate、cron、老客户端）从会话里捡回来
    inherited = TurnRequest(session_id=session.id, message="再问一次")
    await turn_manager._apply_agent_selection(inherited, await sessions.get_session(session.id))
    assert inherited.agent_connection == "ag-1"

    cleared = TurnRequest(session_id=session.id, message="不用子代理了", agent_connection="")
    await turn_manager._apply_agent_selection(cleared, session)
    assert cleared.agent_connection is None
    assert (await sessions.get_session(session.id)).agent_connection is None


def _subagent_wire(call_id: str, block_id: str, text: str, seq: int) -> dict:
    return {
        "type": "subagent_event",
        "turn_id": "turn-1",
        "seq": seq,
        "payload": {
            "call_id": call_id,
            "block_id": block_id,
            "kind": "block",
            "text": text,
            "name": "",
            "args": None,
            "ok": None,
        },
    }


def test_buffer_keeps_only_the_latest_frame_per_block():
    """同一块反复覆盖的帧只留最新一条：一趟咨询上千帧，全留着重连就得重放上千个 dict。"""
    from nnnu.runtime.turn_runtime import TurnRuntimeManager, _TurnExecution

    execution = _TurnExecution(turn_id="turn-1", request=None, bus=None)
    TurnRuntimeManager._buffer(execution, _subagent_wire("call-1", "m1:0", "我来", 1))
    TurnRuntimeManager._buffer(execution, _subagent_wire("call-1", "m1:0", "我来写", 2))
    TurnRuntimeManager._buffer(execution, _subagent_wire("call-1", "m1:0", "我来写个快排", 3))

    assert len(execution.events) == 1
    assert execution.events[0]["payload"]["text"] == "我来写个快排"
    # 留下的是最新那一帧（连它的 seq 一起）：重连按 seq > after_seq 过滤，还是看得见最新全文
    assert execution.events[0]["seq"] == 3

    # 别的块、别的工具调用、别的类型：各占各的格子
    TurnRuntimeManager._buffer(execution, _subagent_wire("call-1", "m1:1", "工具行", 4))
    TurnRuntimeManager._buffer(execution, _subagent_wire("call-2", "m1:0", "另一次咨询", 5))
    TurnRuntimeManager._buffer(execution, {"type": "tool_progress", "seq": 6, "payload": {}})
    TurnRuntimeManager._buffer(
        execution, {"type": "subagent_event", "seq": 7, "payload": {"kind": "status", "text": "?"}}
    )
    assert [event["seq"] for event in execution.events] == [3, 4, 5, 6, 7]
    assert len(execution.events) == 5


async def test_tool_clips_a_long_conclusion(agent_service, tmp_path, repo_prompts):
    """结论直接进下一轮上下文（tool 消息），不截能把预算吃穿。"""
    from nnnu.tools.builtin.subagent_tool import OUTPUT_MAX_CHARS, ConsultSubagentTool

    body = """
import json, sys

sys.stdin.read()
text = "长" * 12000
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": text}), flush=True)
"""
    await _make_connection(agent_service, tmp_path, body, "long.py")
    result = await ConsultSubagentTool().run(_tool_ctx())
    assert result.ok is True
    assert "已截断到 8000 字" in result.output
    assert len(result.output) < OUTPUT_MAX_CHARS + 200
    # 正文末尾那句被截掉了，抬头还在
    assert result.output.startswith("【子代理「测试子代理」的结论】")
