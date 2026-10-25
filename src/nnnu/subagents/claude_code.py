"""Claude Code 无头输出（stream-json）的解析与累积（§7.12）。

上游协议（自研实现，形状照着 `claude -p --output-format stream-json --verbose
--include-partial-messages` 的逐行 JSON 走）：

- `system/init`：会话开始，带 `session_id`；
- `stream_event` → `content_block_delta`（`text_delta` / `thinking_delta`）：增量正文；
- `assistant`：完整消息（`text` / `tool_use` / `thinking` 块）；
- `user`：`tool_result` 块（工具跑完的结果）；
- `result`：终局（`subtype` / `is_error` / `result` 终局正文）；
- 其余（`rate_limit_event` / `control_request` / `control_response`）丢弃。

**两条自己定的规矩**（都为了前端能只做覆盖写）：

1. `block_id` 是一帧的身份：有增量时用 `"{message_id}:{index}"`，只有完整块时退到
   `"msg-{n}:{index}"`——串了块就是内容互相覆盖；
2. **服务端累积正文、发全文快照**，不把增量丢给前端拼：节流窗口（默认 100ms）内只发
   最新一帧，块关闭与完整消息到达时立即补一帧。前端拿到的是「这一块现在长什么样」，
   与收到多少帧无关。

非 JSON 行（CLI 的杂项输出、警告）当状态行；stdout 上的坏行绝不能让这一趟咨询失败。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于类型标注：运行时不把 services 拉进来（subagents 是被它调的）
    from nnnu.services.agents.spec import AgentConnection
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 正文快照的节流窗口：100ms 一帧，界面上看着是连续打字，又不至于每来一个 token 就发一条事件
THROTTLE_S = 0.1
#: 单块正文上限：再长也没人看，而且这些帧会进回合缓冲
BLOCK_TEXT_MAX_CHARS = 20_000
#: 一行工具结果的上限（编译输出那种几万行的，只要头尾）
TOOL_RESULT_MAX_CHARS = 4_000
#: 状态行上限
STATUS_MAX_CHARS = 500
#: 工具参数里单个值保留的长度（进来的是 JSON，模型要写文件时可能是几 KB 的正文）
ARG_VALUE_MAX_CHARS = 160
#: 工具参数最多留几个键
ARG_MAX_KEYS = 6

#: 认得出「会话没了」的错误文本（`--resume` 撞上已过期/不存在的 CLI 会话时要丢掉重开）
STALE_SESSION_MARKERS = (
    "no such session",
    "session not found",
    "invalid session",
    "could not find session",
    "unknown session",
)


@dataclass(slots=True)
class Frame:
    """一帧活动流（runner 把它变成 subagent_event）。"""

    block_id: str
    kind: str  # block | thinking | tool | tool_result | status
    text: str = ""
    name: str = ""
    args: dict[str, Any] | None = None
    ok: bool | None = None


@dataclass(slots=True)
class _Block:
    kind: str  # block（正文）| thinking（思考）
    text: str = ""
    last_emit: float = 0.0
    closed: bool = False


@dataclass(slots=True)
class _State:
    session_id: str = ""
    message_id: str = ""
    ok: bool = True
    stop_reason: str = ""
    result_text: str = ""


def block_id(message_id: str, index: int, *, fallback: int = 0) -> str:
    """块的稳定身份（见模块头第 1 条）。`fallback` 是「没有 message_id」时的消息序号。"""
    return f"{message_id}:{index}" if message_id else f"msg-{fallback}:{index}"


def build_argv(connection: "AgentConnection", *, resume: str = "") -> list[str]:
    """一次咨询的完整命令行（不含程序解析：由 runner 交给 resolve_command）。

    `-p` + `--output-format stream-json` 是无头模式的固定组合；`--verbose` 与
    `--include-partial-messages` 一起才有逐块增量（少了它就只有整条消息，界面上是
    「一段一段蹦出来」）。**prompt 不进 argv**：Windows 上程序可能是 `.cmd`，命令行会被
    cmd.exe 二次解析（`& | > ^ %`），模型写的任务描述里带这些字符就会被截断；
    任务正文一律走 stdin（见 runner）。`--permission-mode` 只来自连接配置，模型碰不到。
    """
    argv = [
        *connection.argv_head(),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        connection.permission_mode,
    ]
    if connection.model_override:
        argv += ["--model", connection.model_override]
    if resume:
        argv += ["--resume", resume]
    return argv


class ClaudeCodeParser:
    """逐行喂 JSON，吐出要发的事件帧（纯逻辑，不碰进程/事件循环，可单测）。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        throttle_s: float = THROTTLE_S,
    ) -> None:
        self._clock = clock
        self._throttle_s = throttle_s
        self._state = _State()
        self._blocks: dict[str, _Block] = {}
        self._message_seq = 0
        self._frame_seq = 0
        self._current_message = ""

    # ---- 结果 ----

    @property
    def session_id(self) -> str:
        """CLI 侧的会话 id（下一轮 `--resume` 用它）。"""
        return self._state.session_id

    @property
    def ok(self) -> bool:
        return self._state.ok

    @property
    def stop_reason(self) -> str:
        """终局子类型（success / error_max_turns / …）；没有 result 事件时为空。"""
        return self._state.stop_reason

    @property
    def result_text(self) -> str:
        """`result` 事件里的终局正文（工具用它当 output；可能为空）。"""
        return self._state.result_text

    @property
    def full_text(self) -> str:
        """所有正文块拼起来的全文（没有 result 事件时的兜底输出）。"""
        parts = [b.text for b in self._blocks.values() if b.kind == "block"]
        return "\n\n".join(part for part in parts if part.strip())

    @property
    def stale_session(self) -> bool:
        """这趟失败是不是因为 `--resume` 指的会话已经没了。"""
        if self._state.ok:
            return False
        text = f"{self._state.result_text} {self._state.stop_reason}".lower()
        return any(marker in text for marker in STALE_SESSION_MARKERS)

    # ---- 输入 ----

    def feed(self, line: str) -> list[Frame]:
        """喂一行 stdout；非 JSON / 认不出的行当状态帧（绝不上抛）。"""
        text = line.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [self._status(text)]
        if not isinstance(payload, dict):
            return [self._status(text)]
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._state.session_id = session_id
        kind = payload.get("type")
        if kind == "stream_event":
            return self._feed_stream_event(payload.get("event"))
        if kind == "assistant":
            return self._feed_assistant(payload.get("message"))
        if kind == "user":
            return self._feed_tool_results(payload.get("message"))
        if kind == "result":
            return self._feed_result(payload)
        if kind == "system":
            return [self._status(_system_line(payload))]
        # rate_limit_event / control_request / control_response 之类的噪音：丢掉
        return []

    def close(self) -> list[Frame]:
        """收尾：把还开着的块补一帧全文（进程被杀时最后一段没走 content_block_stop）。"""
        frames: list[Frame] = []
        for identifier, block in self._blocks.items():
            if not block.closed and block.text:
                block.closed = True
                frames.append(Frame(block_id=identifier, kind=block.kind, text=block.text))
        return frames

    # ---- 各事件 ----

    def _feed_stream_event(self, event: Any) -> list[Frame]:
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                self._message_seq += 1  # 只用来给「没有 id 的块」编号
                self._current_message = str(message["id"])
            return []
        if kind == "content_block_start":
            index = _int(event.get("index"))
            block = event.get("content_block")
            block_type = str((block or {}).get("type") or "")
            if block_type not in ("text", "thinking"):
                return []
            # 起始块里可能已经带了完整文本（短块不发 delta）
            seed = ""
            if block_type == "text":
                seed = str((block or {}).get("text") or "")
            else:
                seed = str((block or {}).get("thinking") or "")
            self._blocks[self._block_key(index)] = _Block(
                kind="block" if block_type == "text" else "thinking",
                text=seed,
                last_emit=self._clock(),
            )
            return []
        if kind == "content_block_delta":
            index = _int(event.get("index"))
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return []
            delta_type = str(delta.get("type") or "")
            if delta_type == "text_delta":
                return self._append(index, str(delta.get("text") or ""), "block")
            if delta_type == "thinking_delta":
                return self._append(index, str(delta.get("thinking") or ""), "thinking")
            return []
        if kind == "content_block_stop":
            return self._flush(self._block_key(_int(event.get("index"))))
        return []

    def _feed_assistant(self, message: Any) -> list[Frame]:
        """完整消息：正文/思考块发全文快照（与增量同形，天然幂等），工具调用发工具帧。"""
        if not isinstance(message, dict):
            return []
        message_id = str(message.get("id") or self._current_message or "")
        if message_id:
            self._current_message = message_id
        frames: list[Frame] = []
        for index, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            identifier = block_id(message_id, index, fallback=self._message_seq)
            if block_type == "text":
                text = _clip(str(block.get("text") or ""), BLOCK_TEXT_MAX_CHARS)
                if text.strip():
                    self._blocks[identifier] = _Block(
                        kind="block", text=text, last_emit=self._clock(), closed=True
                    )
                    frames.append(Frame(block_id=identifier, kind="block", text=text))
            elif block_type == "thinking":
                text = _clip(str(block.get("thinking") or ""), BLOCK_TEXT_MAX_CHARS)
                if text.strip():
                    self._blocks[identifier] = _Block(
                        kind="thinking", text=text, last_emit=self._clock(), closed=True
                    )
                    frames.append(Frame(block_id=identifier, kind="thinking", text=text))
            elif block_type == "tool_use":
                frames.append(
                    Frame(
                        block_id=identifier,
                        kind="tool",
                        name=str(block.get("name") or ""),
                        args=_summarize_args(block.get("input")),
                    )
                )
        return frames

    def _feed_tool_results(self, message: Any) -> list[Frame]:
        if not isinstance(message, dict):
            return []
        frames: list[Frame] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            self._frame_seq += 1
            frames.append(
                Frame(
                    block_id=f"result-{self._frame_seq}",
                    kind="tool_result",
                    text=_clip(_result_text(block.get("content")), TOOL_RESULT_MAX_CHARS),
                    ok=not bool(block.get("is_error")),
                )
            )
        return frames

    def _feed_result(self, payload: dict[str, Any]) -> list[Frame]:
        subtype = str(payload.get("subtype") or "")
        is_error = bool(payload.get("is_error")) or (subtype not in ("", "success"))
        self._state.stop_reason = subtype or ("error" if is_error else "success")
        self._state.ok = not is_error
        text = payload.get("result")
        if isinstance(text, str):
            self._state.result_text = text
        if is_error:
            reason = _clip(text if isinstance(text, str) else "", STATUS_MAX_CHARS)
            return [self._status(f"子代理报错（{self._state.stop_reason}）：{reason or '无输出'}")]
        return []

    # ---- 累积 ----

    def _block_key(self, index: int) -> str:
        return block_id(self._current_message, index, fallback=self._message_seq)

    def _append(self, index: int, text: str, kind: str) -> list[Frame]:
        if not text:
            return []
        key = self._block_key(index)
        block = self._blocks.get(key)
        if block is None:
            block = _Block(kind=kind, last_emit=self._clock())
            self._blocks[key] = block
        block.text = _clip(block.text + text, BLOCK_TEXT_MAX_CHARS)
        now = self._clock()
        if now - block.last_emit < self._throttle_s:
            return []  # 节流窗口内不发：下一帧带上此刻的全文，前端只做覆盖写
        block.last_emit = now
        return [Frame(block_id=key, kind=block.kind, text=block.text)]

    def _flush(self, key: str) -> list[Frame]:
        block = self._blocks.get(key)
        if block is None or block.closed or not block.text:
            return []
        block.closed = True
        block.last_emit = self._clock()
        return [Frame(block_id=key, kind=block.kind, text=block.text)]

    def _status(self, text: str) -> Frame:
        self._frame_seq += 1
        return Frame(
            block_id=f"status-{self._frame_seq}",
            kind="status",
            text=_clip(text, STATUS_MAX_CHARS),
        )


# ---- 小工具 ----


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（内容过长，已截断）"


def _system_line(payload: dict[str, Any]) -> str:
    """system 事件压成一行：init 带上工作目录与模型，别的只报子类型。"""
    subtype = str(payload.get("subtype") or "")
    if subtype == "init":
        parts = [f"子代理会话已开始（{payload.get('model') or '默认模型'}）"]
        if payload.get("cwd"):
            parts.append(f"目录 {payload['cwd']}")
        return "，".join(parts)
    return f"子代理：{subtype or '系统消息'}"


def _result_text(content: Any) -> str:
    """tool_result 的 content 可能是字符串，也可能是 [{type:text,text:…}] 块数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _summarize_args(value: Any) -> dict[str, Any] | None:
    """工具参数压扁成能塞进一行的样子：值转字符串、逐个截断、键数封顶。

    不原样带上：模型写文件时 input 里是整篇正文，一帧几千字白白占缓冲与界面。
    """
    if not isinstance(value, dict) or not value:
        return None
    out: dict[str, Any] = {}
    for key in list(value)[:ARG_MAX_KEYS]:
        item = value[key]
        if isinstance(item, str):
            out[str(key)] = _clip_one_line(item)
        else:
            out[str(key)] = _clip_one_line(json.dumps(item, ensure_ascii=False))
    return out


def _clip_one_line(text: str) -> str:
    return " ".join(text.split())[:ARG_VALUE_MAX_CHARS]


@dataclass(slots=True)
class ConsultResult:
    """一趟咨询的结果（runner 填好交给工具）。"""

    ok: bool = False
    output: str = ""
    stop_reason: str = ""
    session_id: str = ""
    duration_s: float = 0.0
    frame_count: int = 0
    #: runner 自己造的状态帧编号（与 frame_count 分开，免得两套编号互相顶掉）
    seq: int = 0
    stderr_tail: str = ""
    stale_session: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)
