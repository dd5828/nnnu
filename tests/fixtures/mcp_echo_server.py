"""测试用的 MCP stdio 服务器（§7.17）：纯标准库手写的行分隔 JSON-RPC。

为什么不直接拿 SDK 起一个：被测的是**我们的客户端**，服务器这一端最好是照规范
手写出来的最小实现——SDK 起服务器会连带把 SDK 自己的假设（超时、能力协商、
通知时机）再引入一遍，客户端与它对上只能说明「同一个 SDK 自己跟自己对得上」。

协议要点（MCP stdio 传输）：
- stdin 一行一条 JSON-RPC 消息，stdout 一行一条回复；stdout **只许**走协议，
  调试信息一律 stderr（多写一个字都会把协议流弄脏）；
- 带 `id` 的是请求、要回；不带 `id` 的是通知（如 `notifications/initialized`），不回；
- `initialize` 直接回客户端请求的那个 protocolVersion：客户端会校验「服务器说的
  版本我支持吗」，原样奉还是唯一稳定过关的答法。

工具（tools/list 就这三个，E2E 的断言依赖这个数量）：
- `echo`：原样回声；
- `slow_echo`：先按调用方给的 progressToken 发两条 progress 通知，再回声——
  进度通道（§7.17 的 `tool_progress`）靠它验；
- `boom`：返回 `isError: true`（服务器自报失败，不是传输错误）。
环境变量 `MCP_ECHO_EXTRA=1` 时多一个 `die`：直接退进程，用来测「连接断了」。
"""

from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "nnnu-echo", "version": "0.1.0"}

_TEXT_ARG = {"text": {"type": "string"}}
_TOOLS = [
    {
        "name": "echo",
        "description": "把 text 原样回给你",
        "inputSchema": {"type": "object", "properties": _TEXT_ARG, "required": ["text"]},
    },
    {
        "name": "slow_echo",
        "description": "先报两步进度，再把 text 回给你",
        "inputSchema": {"type": "object", "properties": _TEXT_ARG, "required": ["text"]},
    },
    {
        "name": "boom",
        "description": "总是失败（isError），用来测失败路径",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

if os.environ.get("MCP_ECHO_EXTRA") == "1":
    _TOOLS.append(
        {
            "name": "die",
            "description": "直接退出进程（测连接断掉）",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }
    )


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _text(content: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": content}], "isError": is_error}


def _progress(token: object, value: float, total: float, message: str) -> None:
    """一条 progress 通知（没有 progressToken 就不发——规范如此）。"""
    if token is None:
        return
    _send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": token,
                "progress": value,
                "total": total,
                "message": message,
            },
        }
    )


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    text = str(arguments.get("text", ""))
    if name == "echo":
        return _text(f"echo: {text}")
    if name == "slow_echo":
        token = (params.get("_meta") or {}).get("progressToken")
        _progress(token, 1, 2, "第一步")
        time.sleep(0.01)
        _progress(token, 2, 2, "第二步")
        return _text(f"echo: {text}")
    if name == "boom":
        return _text("boom: 服务器自报失败", is_error=True)
    if name == "die" and os.environ.get("MCP_ECHO_EXTRA") == "1":
        # 硬退：不给 SDK 道别的机会，客户端那边的读循环会立刻看到流断掉
        os._exit(3)
    return _text(f"unknown tool: {name}", is_error=True)


def _handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return  # 通知：读掉就行，不回
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        _result(
            request_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "ping":
        _result(request_id, {})
        return
    if method == "tools/list":
        _result(request_id, {"tools": _TOOLS})
        return
    if method == "tools/call":
        _result(request_id, _call_tool(message.get("params") or {}))
        return
    if method == "resources/list":
        _result(request_id, {"resources": []})
        return
    if method == "prompts/list":
        _result(request_id, {"prompts": []})
        return
    _error(request_id, -32601, f"method not found: {method}")


def main() -> None:
    # Windows 上 sys.std* 默认按本地代码页（GBK）编解码，而协议流两端都是 UTF-8：
    # 不掰过来，一句中文就会让客户端抛 UnicodeDecodeError（第一次冒烟就撞上了）。
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - 管道被替换过
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            _handle(message)


if __name__ == "__main__":
    main()
