"""ChatOrchestrator（§6.5）：所有入口的唯一收敛点（WS、未来 Partner 入站都调它）。

流程（与会话生命周期相关的 ensure/持久化由 TurnRuntime 承担——传输层职责）：
1. 解析请求：能力/工具/配置，非法组合 → error 事件 + TurnRejected；
2. 计算 ToolMountFlags → 从 ToolRegistry 取工具集（能力内部消费）；
3. CapabilityRegistry 取能力 → capability.run(ctx, bus)；
4. 统一信封兜底（§6.1）：bus.terminal_emitted 未置 → cost_summary + done；
5. 记忆 L1 轨迹：L1Sink 接口（P8 实现），本阶段 no-op；
6. 返回 TurnResult（response/cost_summary 供传输层持久化）。

要求：编排器本身不含任何能力逻辑。
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from nnnu.core.context import (
    KbRef,
    ModelRef,
    PersonaRef,
    SessionRef,
    UnifiedContext,
)
from nnnu.core.events import StreamEvent, StreamEventType
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import ToolMountFlags

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnResult:
    turn_id: str
    session_id: str
    status: str  # completed | failed | cancelled | rejected
    response: str = ""
    cost_summary: dict[str, Any] = field(default_factory=dict)


class TurnRejected(Exception):
    """请求非法（未知能力/非法组合）：前置校验失败，回合被拒绝。"""


class TurnBusyError(Exception):
    """同 session 已有活动回合（§6.8 并发规则）。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"会话 {session_id} 已有活动回合")
        self.session_id = session_id


class L1Sink(Protocol):
    """记忆 L1 轨迹写入（P8 实现；§6.5 步骤 5 预留）。"""

    async def record_turn(
        self, turn_id: str, session_id: str | None, events: list[StreamEvent]
    ) -> None: ...


class ChatOrchestrator:
    def __init__(
        self,
        *,
        capabilities=None,  # CapabilityRegistry
        tools=None,  # ToolRegistry
        l1_sink: L1Sink | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._tools = tools
        self._l1_sink = l1_sink

    async def handle(self, ctx: UnifiedContext, bus: StreamBus) -> TurnResult:
        capability = None
        if self._capabilities is not None:
            capability = self._capabilities.get(ctx.capability)
        if capability is None:
            message = f"未知能力 {ctx.capability}"
            await bus.emit_error(message=message, recoverable=False)
            raise TurnRejected(message)

        logger.info(
            "回合开始 capability=%s session=%s turn=%s",
            ctx.capability,
            ctx.session.id,
            bus.turn_id,
        )
        try:
            await capability.run(ctx, bus)
        except Exception:
            logger.exception("能力 %s 执行异常", ctx.capability)
            await bus.emit_error(message=f"能力 {ctx.capability} 执行异常", recoverable=False)

        # 统一信封兜底：能力漏发时由编排器补（§6.5 步骤 5）——恰好一次
        summary = ctx.cost.summary()
        if not any(e.type == StreamEventType.COST_SUMMARY for e in bus.history):
            await bus.emit_cost_summary(
                tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
            )
        if not bus.terminal_emitted:
            await bus.emit_done(response="", status="completed")

        if self._l1_sink is not None:
            await self._l1_sink.record_turn(bus.turn_id, ctx.session.id, bus.history)

        return TurnResult(
            turn_id=bus.turn_id,
            session_id=ctx.session.id,
            status="completed",
            cost_summary=summary,
        )


def build_unified_context(
    request,  # TurnRequest
    session,  # services.sessions.models.Session
    user_message,  # services.sessions.models.Message
    session_messages: list,  # 会话历史（含当前消息）
    *,
    language: str,
    model_ref: ModelRef | None = None,
    history_refs: list[SessionRef] | None = None,
) -> UnifiedContext:
    """粘性（模型/persona/kb/语言取会话）vs 一次性（refs 仅当回合）——纯函数。

    附件参数与数量上限已在 TurnRequest 校验层拒绝（传输层 fail-fast）；
    history_refs 由传输层解析（引用会话存在性检查在其处完成）。
    """
    from nnnu.services.llm.factory import parse_model_ref

    if model_ref is None and request.model:
        provider_id, model = parse_model_ref(request.model)
        model_ref = ModelRef(provider=provider_id or "", model=model)
    # §6.3 context_gated：有附件 → attachment_search 自动挂载
    context_flags: set[str] = set()
    if request.attachments:
        context_flags.add("attachment_search")
    return UnifiedContext(
        session=SessionRef(id=session.id, title=session.title),
        capability=request.capability,
        message=user_message,
        attachments=request.attachments,
        history_refs=history_refs or [],
        kb_refs=[KbRef(kb_id=kb_id) for kb_id in request.kb_ids],
        tool_flags=ToolMountFlags(
            context=context_flags,
            forced=set(request.config.get("forced_tools", [])),
            suppressed=set(request.config.get("suppressed_tools", [])),
        ),
        config=dict(request.config),
        persona=(
            PersonaRef(id=session.persona, description=session.persona_description)
            if session.persona
            else None
        ),
        language=language,
        model=model_ref,
        metadata={"session_messages": session_messages},
    )
