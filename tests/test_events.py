"""事件协议：17 类型构造/往返、统一信封与契约检查源。"""

import pytest
from pydantic import ValidationError

from nnnu.core.events import (
    EVENT_TYPES,
    MODEL_BY_TYPE,
    PAYLOAD_FIELDS,
    TERMINAL_TYPES,
    StreamEvent,
    StreamEventType,
)

# 每类型的合法构造参数（覆盖全部 payload 字段的典型值）
VALID_CASES: dict[str, dict] = {
    "turn_start": {"capability": "chat", "model": "deepseek:deepseek-chat"},
    "status": {"stage": "planning", "message": "开始"},
    "content_delta": {"text": "你好"},
    "content_done": {"full_text": "完整回答"},
    "thinking_delta": {"text": "思考中"},
    "thinking_done": {"text": "完整思考"},
    "tool_call": {"tool_name": "add", "args": {"a": 1}, "call_id": "call-1"},
    "tool_result": {"call_id": "call-1", "ok": True, "summary": "=3"},
    "citation": {"sources": [{"doc_id": "d1", "kb": "kb1", "page": 2, "snippet": "..."}]},
    "ask_user": {"question": "选哪个?", "options": ["A", "B"], "ask_id": "ask-1"},
    "ask_user_reply": {"ask_id": "ask-1", "answer": "A"},
    "warning": {"message": "部分检索失败"},
    "error": {"message": "超轮数", "recoverable": False},
    "cost_summary": {
        "tokens": 120,
        "cost": 0.01,
        "per_model": {
            "deepseek-chat": {
                "provider": "deepseek",
                "input_tokens": 100,
                "output_tokens": 20,
                "calls": 2,
                "cost": 0.01,
            }
        },
    },
    "done": {"response": "回答", "status": "completed"},
    "stopped": {},
    "heartbeat": {},
}


def test_all_event_types_have_model_and_fields():
    assert set(MODEL_BY_TYPE) == set(StreamEventType)
    assert set(EVENT_TYPES) == {t.value for t in StreamEventType}
    assert set(PAYLOAD_FIELDS) == {t.value for t in StreamEventType}


@pytest.mark.parametrize("type_name", sorted(VALID_CASES))
def test_make_and_roundtrip(type_name):
    event_type = StreamEventType(type_name)
    event = StreamEvent.make(event_type, "turn-1", session_id="sess-1", **VALID_CASES[type_name])
    assert event.type == event_type
    assert event.turn_id == "turn-1"
    assert event.session_id == "sess-1"
    assert event.event_id.startswith("evt-")
    assert isinstance(event.ts, float)
    # payload 校验后保留原始值（嵌套 dict 原样）
    for key, value in VALID_CASES[type_name].items():
        assert event.payload[key] == value
    # 类型化还原视图与 payload 完全一致
    assert event.payload_model().model_dump() == event.payload


def test_make_defaults_fill_payload_fields():
    event = StreamEvent.make(StreamEventType.HEARTBEAT, "turn-1")
    # payload dict 与还原模型字段一致（线格式确定性）
    assert set(event.payload) == set(PAYLOAD_FIELDS["heartbeat"])


def test_make_invalid_payload_raises():
    with pytest.raises(ValidationError):
        StreamEvent.make(StreamEventType.TOOL_CALL, "turn-1")  # 缺 tool_name/call_id


def test_to_wire_adds_seq():
    event = StreamEvent.make(StreamEventType.CONTENT_DELTA, "turn-1", text="hi")
    wire = event.to_wire(seq=3)
    assert wire["seq"] == 3
    assert wire["type"] == "content_delta"
    assert wire["payload"] == {"text": "hi"}
    # seq 不进入 payload
    assert "seq" not in event.payload


def test_terminal_types_cover_envelope():
    assert set(TERMINAL_TYPES) == {
        StreamEventType.DONE,
        StreamEventType.ERROR,
        StreamEventType.STOPPED,
    }
