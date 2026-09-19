"""统一上下文：字段默认与嵌套引用模型。"""

import pytest
from pydantic import ValidationError

from nnnu.core.context import (
    ModelRef,
    SessionRef,
    UnifiedContext,
)
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.sessions.models import Message


def _ctx(**overrides):
    base = dict(
        session=SessionRef(id="sess-1", title="会话"),
        capability="chat",
        message=Message.new(session_id="sess-1", role="user", content="你好"),
    )
    base.update(overrides)
    return UnifiedContext(**base)


def test_defaults():
    ctx = _ctx()
    assert ctx.attachments == []
    assert ctx.kb_refs == []
    assert ctx.notebook_refs == []
    assert ctx.history_refs == []
    assert ctx.question_refs == []
    assert ctx.config == {}
    assert ctx.persona is None
    assert ctx.language == "zh"
    assert ctx.model is None
    assert isinstance(ctx.cost, CostTracker)
    assert ctx.metadata == {}
    # 每个实例独立 CostTracker（不共享状态）
    assert ctx.cost is not _ctx().cost


def test_model_ref_validation():
    ctx = _ctx(model=ModelRef(provider="deepseek", model="deepseek-chat"))
    assert ctx.model.provider == "deepseek"
    with pytest.raises(ValidationError):
        ModelRef(provider="x")  # 缺 model


def test_message_required():
    with pytest.raises(ValidationError):
        UnifiedContext(session=SessionRef(id="s"), capability="chat")  # 缺 message


def test_cost_tracker_shared_with_capability():
    ctx = _ctx()
    ctx.cost.add_usage(provider="deepseek", model="deepseek-chat", input_tokens=10, output_tokens=5)
    assert ctx.cost.summary()["tokens"] == 15
