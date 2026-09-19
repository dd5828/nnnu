"""DSML 工具调用解析（§6.6）：正文流中夹带工具调用标记时解析执行，正文不丢。

格式（无原生 function calling 的兼容端点输出）：
    <||DSML||function_calls>
    <||DSML||invoke name="tool_name">
    <||DSML||parameter name="key">value</||DSML||parameter>
    </||DSML||invoke>
    </||DSML||function_calls>

- DSMLStreamFilter：增量剥除完整 invoke 块，块外散文即时透出；
  不完整 invoke 缓冲，flush 时仍不完整则原样释放（宁放原文不丢输出）；
- extract_dsml_tool_calls：流结束对全文正则解析，参数按已挂载工具
  schema 声明类型强制（string 值转 int/float/bool/容器）。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ENVELOPE_OPEN = "<||DSML||function_calls>"
ENVELOPE_CLOSE = "</||DSML||function_calls>"
INVOKE_OPEN = "<||DSML||invoke"
INVOKE_CLOSE = "</||DSML||invoke>"
PARAM_OPEN = "<||DSML||parameter"
PARAM_CLOSE = "</||DSML||parameter>"

# 部分标签缓冲上限：超过按普通散文逐字符释放（防畸形输出撑爆缓冲）
MAX_PARTIAL_CHARS = 512


@dataclass(slots=True)
class ParsedToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


class DSMLStreamFilter:
    """增量剥除 DSML 块：正文即时透出，不完整 invoke 缓冲等待闭合。"""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> str:
        buffer = self._pending + chunk
        # 依次剥掉完整的信封开闭标签与 invoke 块
        buffer = buffer.replace(ENVELOPE_OPEN, "").replace(ENVELOPE_CLOSE, "")
        out = ""
        while buffer:
            start = buffer.find(INVOKE_OPEN)
            if start == -1:
                out += buffer
                buffer = ""
                break
            out += buffer[:start]
            rest = buffer[start:]
            end = rest.find(INVOKE_CLOSE)
            if end == -1:
                # 不完整 invoke：缓冲等闭合；超上限按散文释放防撑爆
                if len(rest) > MAX_PARTIAL_CHARS:
                    out += rest[:MAX_PARTIAL_CHARS]
                    rest = rest[MAX_PARTIAL_CHARS:]
                    self._pending = rest
                    return out
                self._pending = rest
                return out
            # 完整块剥掉（含闭合标签），继续找下一个
            buffer = rest[end + len(INVOKE_CLOSE) :]
        self._pending = ""
        return out

    def flush(self) -> str:
        """流尾：残留原样释放（宁可把畸形标记给用户看也不丢 provider 输出）。"""
        leftover = self._pending
        self._pending = ""
        return leftover


def extract_dsml_tool_calls(
    text: str, tool_schemas: dict[str, dict[str, Any]] | None = None
) -> tuple[list[ParsedToolCall], str]:
    """全文解析：返回 (调用清单, 清理后的正文)。

    参数按工具 schema 声明的 properties 类型强制；解析失败返回空调用列表。
    """
    pattern = re.compile(
        rf"{re.escape(INVOKE_OPEN)}\s+name=\"([^\"]+)\">(.*?){re.escape(INVOKE_CLOSE)}",
        re.DOTALL,
    )
    calls: list[ParsedToolCall] = []
    for index, match in enumerate(pattern.finditer(text)):
        name = match.group(1)
        body = match.group(2)
        arguments = _parse_arguments(body, name, tool_schemas)
        calls.append(ParsedToolCall(name=name, arguments=arguments, call_id=f"dsml-{index}"))
    cleaned = pattern.sub("", text)
    return calls, cleaned


def _parse_arguments(
    body: str, tool_name: str, tool_schemas: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    """优先整段 JSON；否则按 <||DSML||parameter> 对解析；按 schema 类型强制。"""
    stripped = body.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return _coerce(parsed, tool_name, tool_schemas)
        except json.JSONDecodeError:
            pass
    arguments: dict[str, Any] = {}
    param_pattern = re.compile(
        rf"{re.escape(PARAM_OPEN)}\s+name=\"([^\"]+)\">(.*?){re.escape(PARAM_CLOSE)}",
        re.DOTALL,
    )
    for match in param_pattern.finditer(body):
        key = match.group(1)
        raw_value = match.group(2).strip()
        arguments[key] = _coerce_scalar(raw_value, key, tool_name, tool_schemas)
    return arguments


def _coerce(
    values: dict[str, Any], tool_name: str, tool_schemas: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    return {
        key: _coerce_scalar(value, key, tool_name, tool_schemas) for key, value in values.items()
    }


def _coerce_scalar(
    value: Any, key: str, tool_name: str, tool_schemas: dict[str, dict[str, Any]] | None
) -> Any:
    """string 值按 schema 声明类型强制（int/float/bool）；无 schema 尊重原值。"""
    if not isinstance(value, str):
        return value
    declared_type = None
    if tool_schemas:
        schema = tool_schemas.get(tool_name)
        if schema:
            declared_type = (schema.get("properties") or {}).get(key, {}).get("type")
    if declared_type in ("integer", "number", "boolean"):
        try:
            if declared_type == "integer":
                return int(value)
            if declared_type == "number":
                return float(value)
            if declared_type == "boolean":
                return value.strip().lower() in ("true", "1", "yes")
        except ValueError:
            logger.warning("DSML 参数 %s=%r 无法按 %s 强制，保留原串", key, value, declared_type)
    return value
