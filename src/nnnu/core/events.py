"""事件协议（§6.1）：StreamEvent 全量类型清单与统一信封。

- StreamEvent.make() 是唯一构造入口：payload 经类型化模型校验后以 dict 存储；
- payload_model() 供消费者还原类型化视图（测试/序列化消费）；
- 信封收尾规则（§6.1）：每回合无论成败，最后必须发出 cost_summary 与
  done|error|stopped 之一——由 StreamBus.terminal_emitted 标志防双发；
- EVENT_TYPES / PAYLOAD_FIELDS 是 web/types/stream.ts 契约检查的单一数据源。
"""

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from nnnu.core.ids import new_id


class StreamEventType(StrEnum):
    TURN_START = "turn_start"
    STATUS = "status"
    CONTENT_DELTA = "content_delta"
    CONTENT_DONE = "content_done"
    THINKING_DELTA = "thinking_delta"
    THINKING_DONE = "thinking_done"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CITATION = "citation"
    ASK_USER = "ask_user"
    ASK_USER_REPLY = "ask_user_reply"
    WARNING = "warning"
    ERROR = "error"
    COST_SUMMARY = "cost_summary"
    DONE = "done"
    STOPPED = "stopped"
    HEARTBEAT = "heartbeat"


# ---- payload 模型（snake_case，TS 侧 web/types/stream.ts 同名镜像） ----


class TurnStartPayload(BaseModel):
    capability: str
    model: str | None = None


class StatusPayload(BaseModel):
    stage: str
    message: str = ""


class ContentDeltaPayload(BaseModel):
    text: str


class ContentDonePayload(BaseModel):
    full_text: str


class ThinkingDeltaPayload(BaseModel):
    text: str


class ThinkingDonePayload(BaseModel):
    text: str


class ToolCallPayload(BaseModel):
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: str


class ToolResultPayload(BaseModel):
    call_id: str
    ok: bool
    summary: str
    detail: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


class CitationSource(BaseModel):
    doc_id: str
    kb: str
    page: int | None = None
    snippet: str = ""


class CitationPayload(BaseModel):
    sources: list[CitationSource] = Field(default_factory=list)


class AskUserOption(BaseModel):
    """卡片选项：短标签 + 可选长说明。

    - label 是「用户点它就发回服务端的那个字符串」（判分要靠它精确对上答案键）；
    - 模型-authored 的朴素写法（纯字符串数组）在前校验里归一成
      {label: 原串, description: ""}，老调用方不用改。
    """

    label: str
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"label": value}
        return value


class AskUserPayload(BaseModel):
    question: str
    options: list[AskUserOption] = Field(default_factory=list)
    ask_id: str
    # 卡片是否自带自由文本输入框（定性评定、开放作答用；纯选项卡为 False）
    allow_free_text: bool = False
    # 服务端拼的展示副标题（如「节点《…》· 第 2/3 题」）。只进文案不进身份：
    # 「这张卡考的是哪道题」的权威绑定留在服务端（learning_interactions 行）。
    context: str = ""


class AskUserReplyPayload(BaseModel):
    ask_id: str
    answer: str


class WarningPayload(BaseModel):
    message: str


class ErrorPayload(BaseModel):
    message: str
    recoverable: bool = False


class ModelCostBreakdown(BaseModel):
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost: float = 0.0


class CostSummaryPayload(BaseModel):
    tokens: int = 0
    cost: float = 0.0
    per_model: dict[str, ModelCostBreakdown] = Field(default_factory=dict)


class ToolTrace(BaseModel):
    tool_name: str
    call_id: str
    ok: bool
    summary: str = ""
    # 小结构展示数据（答题结果卡、引用来源等）：跟着历史活下来，刷新后仍是卡；
    # 超限的（imagegen 的 data URI）在 agent_loop 就丢掉了，这里自然是 None
    detail: dict[str, Any] | None = None


class DonePayload(BaseModel):
    response: str
    status: str = "completed"  # completed | failed | synthesized 语义见传输层
    citations: list[CitationSource] = Field(default_factory=list)
    tool_calls: list[ToolTrace] = Field(default_factory=list)
    synthesized: bool = False


class StoppedPayload(BaseModel):
    pass


class HeartbeatPayload(BaseModel):
    pass


# ---- StreamEvent ----


# 类型 → payload 模型：事件构造/还原与契约检查的单一映射
MODEL_BY_TYPE: dict[StreamEventType, type[BaseModel]] = {
    StreamEventType.TURN_START: TurnStartPayload,
    StreamEventType.STATUS: StatusPayload,
    StreamEventType.CONTENT_DELTA: ContentDeltaPayload,
    StreamEventType.CONTENT_DONE: ContentDonePayload,
    StreamEventType.THINKING_DELTA: ThinkingDeltaPayload,
    StreamEventType.THINKING_DONE: ThinkingDonePayload,
    StreamEventType.TOOL_CALL: ToolCallPayload,
    StreamEventType.TOOL_RESULT: ToolResultPayload,
    StreamEventType.CITATION: CitationPayload,
    StreamEventType.ASK_USER: AskUserPayload,
    StreamEventType.ASK_USER_REPLY: AskUserReplyPayload,
    StreamEventType.WARNING: WarningPayload,
    StreamEventType.ERROR: ErrorPayload,
    StreamEventType.COST_SUMMARY: CostSummaryPayload,
    StreamEventType.DONE: DonePayload,
    StreamEventType.STOPPED: StoppedPayload,
    StreamEventType.HEARTBEAT: HeartbeatPayload,
}

# 契约检查源（tests/test_contract_stream.py ↔ web/types/stream.ts）
EVENT_TYPES: tuple[str, ...] = tuple(t.value for t in StreamEventType)
PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    t.value: tuple(MODEL_BY_TYPE[t].model_fields) for t in StreamEventType
}

# 回合边界/终局事件：传输层（TurnRuntime）用其判定回合收尾
TERMINAL_TYPES: tuple[StreamEventType, ...] = (
    StreamEventType.DONE,
    StreamEventType.ERROR,
    StreamEventType.STOPPED,
)


class StreamEvent(BaseModel):
    """§6.1 事件协议：event_id/turn_id/session_id/type/payload/ts。"""

    event_id: str
    turn_id: str
    session_id: str | None = None
    type: StreamEventType
    payload: dict[str, Any]
    ts: float

    @classmethod
    def make(
        cls,
        type: StreamEventType,
        turn_id: str,
        *,
        session_id: str | None = None,
        **payload_kwargs: Any,
    ) -> "StreamEvent":
        """唯一构造入口：payload 经类型化模型校验后存 dict。"""
        model = MODEL_BY_TYPE[type]
        validated = model(**payload_kwargs)
        return cls(
            event_id=new_id("evt"),
            turn_id=turn_id,
            session_id=session_id,
            type=type,
            payload=validated.model_dump(),
            ts=time.time(),
        )

    def payload_model(self) -> BaseModel:
        """还原类型化 payload（测试与消费者用）。"""
        return MODEL_BY_TYPE[self.type](**self.payload)

    def to_wire(self, *, seq: int = 0) -> dict[str, Any]:
        """传输层信封：事件字段 + 单调 seq（seq 由 TurnRuntime 分配，不落 payload）。"""
        return {**self.model_dump(), "seq": seq}
