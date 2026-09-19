"""契约检查（§12.1）：事件类型与 TS 类型双向比对。

Python 侧数据源：nnnu/core/events.py 的 EVENT_TYPES / PAYLOAD_FIELDS；
TS 侧：web/types/stream.ts（联合类型一行 + payload 接口每字段一行）。
"""

import re
from pathlib import Path

from nnnu.core.events import EVENT_TYPES, PAYLOAD_FIELDS

STREAM_TS = Path(__file__).resolve().parents[1] / "web" / "types" / "stream.ts"

# 跳过契约比对的键（传输层字段/嵌套模型名），其余 payload 字段必须镜像
PAYLOAD_SKIP_FIELDS: dict[str, set[str]] = {}
# TS 类型名：payload 模型名 → 接口名（蛇形 payload 键 → TS 接口）
TS_TYPE_NAMES = {
    "turn_start": "TurnStartPayload",
    "status": "StatusPayload",
    "content_delta": "ContentDeltaPayload",
    "content_done": "ContentDonePayload",
    "thinking_delta": "ThinkingDeltaPayload",
    "thinking_done": "ThinkingDonePayload",
    "tool_call": "ToolCallPayload",
    "tool_result": "ToolResultPayload",
    "citation": "CitationPayload",
    "ask_user": "AskUserPayload",
    "ask_user_reply": "AskUserReplyPayload",
    "warning": "WarningPayload",
    "error": "ErrorPayload",
    "cost_summary": "CostSummaryPayload",
    "done": "DonePayload",
    "stopped": "StoppedPayload",
    "heartbeat": "HeartbeatPayload",
}


def _ts_content() -> str:
    return STREAM_TS.read_text(encoding="utf-8")


def _ts_union_types() -> set[str]:
    match = re.search(r"export type StreamEventType =\s*(.*?);", _ts_content(), re.DOTALL)
    assert match, "stream.ts 缺少 StreamEventType 联合类型"
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def _ts_interface_fields(interface: str) -> set[str]:
    match = re.search(rf"export interface {interface} \{{\s*(.*?)\}}", _ts_content(), re.DOTALL)
    assert match, f"stream.ts 缺少接口 {interface}"
    return set(re.findall(r"^\s*(\w+)\s*:", match.group(1), re.MULTILINE))


def test_event_types_match_ts():
    """事件类型集合双向一致。"""
    ts_types = _ts_union_types()
    assert set(EVENT_TYPES) == ts_types, (
        f"Python 独有: {set(EVENT_TYPES) - ts_types}, TS 独有: {ts_types - set(EVENT_TYPES)}"
    )


def test_payload_fields_match_ts():
    """每个 payload 的字段名在 TS 接口中双向一致。"""
    for type_name, fields in PAYLOAD_FIELDS.items():
        interface = TS_TYPE_NAMES[type_name]
        ts_fields = _ts_interface_fields(interface)
        skip = PAYLOAD_SKIP_FIELDS.get(type_name, set())
        py_fields = set(fields) - skip
        assert py_fields == ts_fields, (
            f"{type_name}: Python 独有 {py_fields - ts_fields}, TS 独有 {ts_fields - py_fields}"
        )
