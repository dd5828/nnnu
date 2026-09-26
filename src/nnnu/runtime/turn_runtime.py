"""TurnRuntime（§7.20 传输层缝合）：回合生命周期与 WS 之间的唯一桥梁。

职责（对照参考仓库 turn_runtime 的设计意图，全部自研）：
- 互斥：同 session 单活动回合（纯内存 _active_by_session，单进程部署）；
- 事件缓冲：per-turn 内存事件带单调 seq（§7.20 事件有序），回合内任意时刻
  可重放（断线重连补发）；回合结束迁入 _finished LRU 供迟来重放；
- 订阅：每订阅者一队列，None 哨兵收尾；resume 三源合流（运行中回放 /
  已结束回放 / 会话快照合成 done）；
- ask_user：注入 ctx.metadata["ask_user_fn"]——emit ask_user、阻塞等待答复，
  无订阅者时 5 分钟超时空答复（§7.20 防挂起），订阅者到达取消计时；
- stop：task.cancel() → 循环 emit stopped → best-effort 持久化已产出内容；
- 信封兜底：bus.terminal_emitted 未置 → cost_summary + done（防能力漏发）；
- 成本落库：回合结束写入 usage_records（§6.9 验收④）。
"""

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from nnnu.core.context import Attachment, SessionRef
from nnnu.core.events import StreamEventType
from nnnu.core.ids import new_id
from nnnu.core.stream_bus import StreamBus
from nnnu.runtime.log_ctx import turn_log_context
from nnnu.runtime.orchestrator import (
    ChatOrchestrator,
    TurnBusyError,
    TurnRejected,
    build_unified_context,
)
from nnnu.services.cost.service import CostService
from nnnu.services.files.service import MAX_ATTACHMENTS_PER_TURN
from nnnu.services.sessions.models import Message
from nnnu.services.sessions.service import SessionManager

logger = logging.getLogger(__name__)

ASK_TIMEOUT_S = 300.0  # §7.20：ask_user 无前端连接 5 分钟超时空答复
FINISHED_LRU_SIZE = 100  # 已结束回合的迟来重放上限


class TurnRefs(BaseModel):
    """一次性引用（§6.7 仅当回合有效）：P2 先落地历史会话；笔记本/题库/书页
    随 P9/P10 实体扩展字段，未知字段宽容。"""

    model_config = ConfigDict(extra="ignore")

    sessions: list[str] = field(default_factory=list)  # 引用会话 id


class TurnRequest(BaseModel):
    """WS chat 入站与 REST 兜底的统一请求模型（§7.20；未知字段宽容）。"""

    model_config = ConfigDict(extra="ignore")

    session_id: str | None = None
    capability: str = "chat"
    message: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    kb_ids: list[str] = field(default_factory=list)
    refs: TurnRefs = field(default_factory=TurnRefs)
    config: dict[str, Any] = field(default_factory=dict)
    language: str | None = None
    model: str | None = None
    # 运行时内部标志：regenerate 复用原 user 消息，不重复落库（§6.8）
    persist_user_message: bool = True

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: object) -> str | None:
        """会话级模型选择（§6.10）：null/空串 = 清除（跟随设置默认）；非空必须是
        'provider:model' 且 provider 在册——选择器只可能产出规范串，其余按坏客户端
        fail-fast（与附件数量校验同一条线）。模型名不在注册表快照里则放行：
        快照不是白名单（§6.10），兼容端点上的自有模型照样能用。"""
        # 惰性导入，同 build_unified_context
        from nnnu.services.llm.factory import normalize_model_ref

        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("model 必须是 'provider:model' 字符串（null = 跟随设置默认）")
        text = value.strip()
        if not text:
            return None
        normalized = normalize_model_ref(text)
        if normalized is None:
            from nnnu.services.llm.provider_registry import build_registry

            known = "、".join(f"{spec.id}:…" for spec in build_registry())
            raise ValueError(
                f"未知模型引用 {value!r}：需 'provider:model'，provider 取 {known} 之一"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_attachments(self) -> "TurnRequest":
        if len(self.attachments) > MAX_ATTACHMENTS_PER_TURN:
            raise ValueError(f"单回合附件数量超过上限（{MAX_ATTACHMENTS_PER_TURN} 个）")
        return self


@dataclass(slots=True)
class _TurnExecution:
    turn_id: str
    request: TurnRequest
    bus: StreamBus
    task: asyncio.Task | None = None
    session_id: str | None = None
    events: list[dict] = field(default_factory=list)  # wire 事件（含单调 seq）
    next_seq: int = 1
    subscribers: list[asyncio.Queue[dict | None]] = field(default_factory=list)
    awaiting_user_reply: bool = False
    pending_ask_id: str | None = None
    reply_queue: asyncio.Queue[tuple[str, str]] | None = None
    reply_timeout_task: asyncio.Task | None = None


class TurnRuntimeManager:
    def __init__(
        self,
        *,
        sessions: SessionManager,
        costs: CostService,
        orchestrator: ChatOrchestrator,
    ) -> None:
        self._sessions = sessions
        self._costs = costs
        self._orchestrator = orchestrator
        self._executions: dict[str, _TurnExecution] = {}
        self._active_by_session: dict[str, str] = {}
        self._finished: OrderedDict[str, _TurnExecution] = OrderedDict()

    # ---- 入站 ----

    async def start_turn(self, request: TurnRequest) -> dict:
        """发起回合：互斥检查（同步）→ 注册 execution → 后台任务。"""
        if request.session_id is not None:
            active_turn = self._active_by_session.get(request.session_id)
            if active_turn is not None and active_turn in self._executions:
                raise TurnBusyError(request.session_id)
        turn_id = new_id("turn")
        bus = StreamBus(turn_id)
        execution = _TurnExecution(turn_id=turn_id, request=request, bus=bus)
        self._executions[turn_id] = execution
        execution.task = asyncio.create_task(self._run_turn(execution))
        return {"turn_id": turn_id, "session_id": request.session_id}

    async def stop_turn(self, turn_id: str) -> bool:
        execution = self._executions.get(turn_id)
        if execution is None or execution.task is None or execution.task.done():
            return False
        execution.task.cancel()
        try:
            await execution.task
        except asyncio.CancelledError:
            pass
        return True

    async def submit_user_reply(self, turn_id: str, ask_id: str, answer: str) -> bool:
        """ask_user 答复（§7.20：任意连接可投，不绑定原连接）。"""
        execution = self._executions.get(turn_id)
        if execution is None or not execution.awaiting_user_reply:
            return False
        if execution.pending_ask_id is not None and execution.pending_ask_id != ask_id:
            return False
        if execution.reply_queue is None:
            return False
        execution.reply_queue.put_nowait((ask_id, answer))
        return True

    async def regenerate(self, session_id: str) -> dict:
        """丢弃末轮 assistant 消息，以同一用户消息重跑（§6.8）。"""
        active_turn = self._active_by_session.get(session_id)
        if active_turn is not None and active_turn in self._executions:
            raise TurnBusyError(session_id)
        last_user = await self._sessions.get_last_user_message(session_id)
        if last_user is None:
            raise TurnRejected(f"会话 {session_id} 无用户消息可重新生成")
        await self._sessions.delete_last_assistant(session_id)
        # 能力跟着会话走（§6.4）：不带的话解题会话点「重新生成」会退回聊天
        request = TurnRequest(
            session_id=session_id,
            message=last_user.content,
            capability=await self.session_capability(session_id),
            persist_user_message=False,
        )
        return await self.start_turn(request)

    async def session_capability(self, session_id: str | None) -> str:
        """会话记住的能力名（cron 等非前端入口用它补上 capability，没会话时回退 chat）。"""
        if not session_id:
            return "chat"
        session = await self._sessions.get_session(session_id)
        return session.capability if session is not None else "chat"

    # ---- 订阅与重放（§7.20 resume 三源合流） ----

    async def subscribe_turn(self, turn_id: str, after_seq: int = 0) -> AsyncIterator[dict]:
        execution = self._executions.get(turn_id) or self._finished.get(turn_id)
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        # 运行中：回放 + 注册实时队列；已结束（LRU 内）：纯回放后即止
        replay = (
            [e for e in execution.events if e["seq"] > after_seq] if execution is not None else []
        )
        if execution is not None and execution.task is not None and not execution.task.done():
            execution.subscribers.append(queue)
            self._cancel_ask_timeout_on_subscribe(execution)
            live = True
        else:
            live = False
        for event in replay:
            yield event
        if live:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

    async def subscribe_session(self, session_id: str, after_seq: int = 0) -> AsyncIterator[dict]:
        """按 session 订阅：活动回合 → 回放+实时；否则从会话快照合成终止序列。"""
        active_turn = self._active_by_session.get(session_id)
        if active_turn is not None and active_turn in self._executions:
            async for event in self.subscribe_turn(active_turn, after_seq):
                yield event
            return
        # 已结束/跨重启：从最后 assistant 消息合成（§7.20 补发缓存事件的降级面）
        last = await self._sessions.get_last_message(session_id)
        if last is None or last.role != "assistant":
            yield {
                "type": StreamEventType.ERROR.value,
                "session_id": session_id,
                "turn_id": "",
                "seq": 0,
                "payload": {
                    "message": "会话无已完成回合可恢复",
                    "recoverable": False,
                },
                "event_id": new_id("evt"),
                "ts": 0.0,
            }
            return
        wire = [
            {
                "type": StreamEventType.CONTENT_DONE.value,
                "payload": {"full_text": last.content},
                "session_id": session_id,
                "turn_id": "",
                "seq": after_seq + 1,
                "event_id": new_id("evt"),
                "ts": last.created_at,
            },
            {
                "type": StreamEventType.DONE.value,
                "payload": {"response": last.content, "status": "completed", "synthesized": True},
                "session_id": session_id,
                "turn_id": "",
                "seq": after_seq + 2,
                "event_id": new_id("evt"),
                "ts": last.created_at,
            },
        ]
        for event in wire:
            yield event

    def subscriber_count(self, turn_id: str) -> int:
        execution = self._executions.get(turn_id)
        return len(execution.subscribers) if execution else 0

    def active_turn_for(self, session_id: str) -> str | None:
        return self._active_by_session.get(session_id)

    # ---- 回合执行 ----

    async def _apply_kb_selection(self, request: TurnRequest, session) -> None:
        """知识库选择（§7.9，粘性对齐上游）：显式带 kb_ids 的请求全量替换并落库（前端
        每条消息都带当前选择，所以空列表就是"取消选择"）；没带 kb_ids 的请求（regenerate、
        cron 回合、老客户端）沿用会话里的选择，不回写。落库失败不阻断回合。"""
        if "kb_ids" in request.model_fields_set:
            try:
                await self._sessions.set_kb_ids(session.id, list(request.kb_ids))
            except Exception:
                logger.warning("知识库选择持久化失败 session=%s", session.id, exc_info=True)
        else:
            request.kb_ids = list(session.kb_ids)

    async def _apply_model_selection(self, request: TurnRequest, session) -> None:
        """模型选择（§6.10，粘性对齐上游）：显式带 model 的请求全量替换并落库（null/空串 =
        清除，回到设置默认）；没带 model 的请求（regenerate、cron 回合、老客户端）沿用
        会话里的选择。会话里存的旧值若已不可用（provider 不在册/模型为空），告警后丢弃
        回退设置默认——粘性值不该把会话搞死。落库失败不阻断回合（同 _apply_kb_selection）。"""
        from nnnu.services.llm.factory import normalize_model_ref

        if "model" in request.model_fields_set:
            ref = normalize_model_ref(request.model)
            try:
                await self._sessions.set_model(session.id, ref)
            except Exception:
                logger.warning("模型选择持久化失败 session=%s", session.id, exc_info=True)
            request.model = ref
        else:
            ref = normalize_model_ref(session.model)
            if session.model and ref is None:
                logger.warning(
                    "会话 %s 存的模型 %r 已不可用，回退设置默认", session.id, session.model
                )
            request.model = ref

    async def _apply_capability_selection(self, request: TurnRequest, session) -> None:
        """能力选择（§6.4 粘性）：显式带 capability 的请求（前端每条消息都带）落库，切走再
        回来还是它；没带的（老客户端）沿用会话里存的。能力名合法性由编排器兜底——未知能力
        在那里报 error + TurnRejected，这里不做白名单（注册表是快照，不该当白名单用）。"""
        if "capability" in request.model_fields_set:
            try:
                await self._sessions.set_capability(session.id, request.capability)
            except Exception:
                logger.warning("能力选择持久化失败 session=%s", session.id, exc_info=True)
        else:
            request.capability = session.capability

    async def _run_turn(self, execution: _TurnExecution) -> None:
        """回合主流程：上下文 → 编排器 → 持久化 → 收尾（单出口收尾，防重复落库）。"""
        request = execution.request
        session = None
        user_message: Message | None = None
        relay = asyncio.create_task(self._relay(execution))
        try:
            with turn_log_context(execution.turn_id, request.session_id):
                session = await self._sessions.ensure_session(
                    request.session_id,
                    capability=request.capability,
                    language=request.language or "zh",
                )
                execution.session_id = session.id
                execution.bus.session_id = session.id  # bus 创建时 session 未知，此处回填
                # 新会话同样注册：resume（按 session 订阅）依赖此映射找到活动回合
                self._active_by_session[session.id] = execution.turn_id
                await self._apply_kb_selection(request, session)
                await self._apply_model_selection(request, session)
                await self._apply_capability_selection(request, session)
                # session 解析后重入日志上下文：后续日志带真实 session_id
                with turn_log_context(execution.turn_id, session.id):
                    await self._run_turn_body(execution, request, session, relay)
                # 正常路径落到 else 分支统一收尾
        except asyncio.CancelledError:
            logger.info("回合被取消 turn=%s", execution.turn_id)
            try:
                await execution.bus.emit_stopped()
            except Exception:
                pass
            await self._finalize_turn(execution, session, user_message, relay, cancelled=True)
            raise
        except Exception:
            logger.exception("回合执行异常 turn=%s", execution.turn_id)
            try:
                await execution.bus.emit_error(message="回合执行异常", recoverable=False)
            except Exception:
                pass
            await self._finalize_turn(execution, session, user_message, relay)
        else:
            await self._finalize_turn(execution, session, user_message, relay)

    async def _run_turn_body(
        self, execution: _TurnExecution, request: TurnRequest, session, relay
    ) -> None:
        """session 解析后的回合主体（异常上抛，收尾全归 _run_turn 单出口）。"""
        user_message: Message | None
        if request.persist_user_message:
            # 用户消息落库（ask_user 挂起时对会话列表可见）+ 标题
            user_message = Message.new(session_id=session.id, role="user", content=request.message)
            await self._sessions.append_message(user_message)
            await self._sessions.set_title_from_first_user(session.id)
        else:
            # regenerate：复用末条 user 消息为快照（§6.8）
            user_message = await self._sessions.get_last_user_message(session.id)
            if user_message is None:
                raise TurnRejected(f"会话 {session.id} 无用户消息可重新生成")
        session_messages = await self._sessions.list_messages(session.id)

        await execution.bus.emit_turn_start(capability=request.capability, model=request.model)
        history_refs, history_transcripts = await self._resolve_history_refs(request, execution.bus)
        ctx = build_unified_context(
            request,
            session,
            user_message,
            session_messages,
            language=request.language or session.language,
            history_refs=history_refs,
        )
        ctx.metadata["history_ref_transcripts"] = history_transcripts
        ctx.metadata["ask_user_fn"] = self._make_ask_user_fn(execution)
        await self._orchestrator.handle(ctx, execution.bus)

    async def _resolve_history_refs(
        self, request: TurnRequest, bus: StreamBus
    ) -> tuple[list[SessionRef], list[dict[str, Any]]]:
        """一次性引用：历史会话转录装入 metadata 供能力注入（§7.1）。

        引用的会话不存在 → warning 跳过，不阻断回合（宽容语义）。
        """
        transcripts: list[dict[str, Any]] = []
        resolved: list[SessionRef] = []
        for session_id in request.refs.sessions:
            ref_session = await self._sessions.get_session(session_id)
            if ref_session is None:
                await bus.emit_warning(message=f"引用的会话 {session_id} 不存在，已忽略")
                continue
            messages = await self._sessions.list_messages(session_id)
            resolved.append(SessionRef(id=ref_session.id, title=ref_session.title))
            transcripts.append(
                {
                    "id": ref_session.id,
                    "title": ref_session.title,
                    "messages": [message.model_dump() for message in messages],
                }
            )
        return resolved, transcripts

    async def _relay(self, execution: _TurnExecution) -> None:
        """bus → wire（补 seq）→ 缓冲 + 订阅者扇出。"""
        async for event in execution.bus.subscribe():
            wire = event.to_wire(seq=execution.next_seq)
            execution.next_seq += 1
            execution.events.append(wire)
            for queue in execution.subscribers:
                queue.put_nowait(wire)

    async def _finalize_turn(
        self,
        execution: _TurnExecution,
        session,
        user_message: Message | None,
        relay: asyncio.Task | None,
        *,
        cancelled: bool = False,
    ) -> None:
        """统一收尾（每步单独 suppress：收尾状态必须落库，不因单步失败中断）。"""
        bus = execution.bus
        # 1) 信封兜底（§6.5）：cost_summary + done 未发才补
        try:
            if not bus.terminal_emitted:
                await bus.emit_done(response="", status="cancelled" if cancelled else "completed")
        except Exception:
            logger.exception("信封兜底失败 turn=%s", execution.turn_id)
        # 2) 关闭总线并等 relay 排空——缓冲里的终局事件是持久化还原的依据
        try:
            bus.mark_closed()
        except Exception:
            pass
        if relay is not None:
            try:
                await relay
            except Exception:
                pass
        # 3) assistant 消息持久化（best-effort，含 partial）
        try:
            if session is not None:
                await self._persist_assistant(execution, session.id)
        except Exception:
            logger.exception("assistant 消息持久化失败 turn=%s", execution.turn_id)
        # 4) 会话活跃时间
        try:
            if session is not None:
                await self._sessions.touch_session(session.id)
        except Exception:
            logger.exception("会话 touch 失败")
        # 5) 成本落库（验收④）
        try:
            if session is not None:
                cost_event = self._last_event(execution, StreamEventType.COST_SUMMARY)
                if cost_event is not None and cost_event["payload"].get("per_model"):
                    await self._costs.record_turn(
                        session_id=session.id,
                        turn_id=execution.turn_id,
                        summary=cost_event["payload"],
                    )
        except Exception:
            logger.exception("成本落库失败 turn=%s", execution.turn_id)
        # 6) 执行迁入 _finished LRU（迟来重放）
        self._executions.pop(execution.turn_id, None)
        if session is not None:
            self._active_by_session.pop(session.id, None)
        self._finished[execution.turn_id] = execution
        self._finished.move_to_end(execution.turn_id)
        while len(self._finished) > FINISHED_LRU_SIZE:
            self._finished.popitem(last=False)
        # 7) 最后才投订阅者哨兵：订阅方（REST 收集/WS 转发）结束即全量收尾完成
        for queue in execution.subscribers:
            queue.put_nowait(None)
        execution.subscribers.clear()

    async def _persist_assistant(self, execution: _TurnExecution, session_id: str) -> None:
        """从事件缓冲还原 assistant 消息（无终局内容时不落空行）。"""
        done = self._last_event(execution, StreamEventType.DONE)
        response = ""
        tool_calls: list[dict] = []
        citations: list[dict] = []
        if done is not None:
            response = done["payload"].get("response", "")
            tool_calls = done["payload"].get("tool_calls", [])
            citations = done["payload"].get("citations", [])
        if not response:
            content_done = self._last_event(execution, StreamEventType.CONTENT_DONE)
            response = content_done["payload"]["full_text"] if content_done else ""
        thinking_parts = [
            e["payload"]["text"]
            for e in execution.events
            if e["type"] == StreamEventType.THINKING_DONE.value
        ]
        cost = self._last_event(execution, StreamEventType.COST_SUMMARY)
        if not response and not thinking_parts and not tool_calls and not citations:
            return  # 无任何产出（如立刻停止）不落空 assistant 行
        message = Message.new(
            session_id=session_id,
            role="assistant",
            content=response,
            thinking="".join(thinking_parts) or None,
            tool_calls=tool_calls,
            citations=citations,
            cost=cost["payload"] if cost else None,
        )
        await self._sessions.append_message(message)

    @staticmethod
    def _last_event(execution: _TurnExecution, type: StreamEventType) -> dict | None:
        for event in reversed(execution.events):
            if event["type"] == type.value:
                return event
        return None

    # ---- ask_user ----

    def _make_ask_user_fn(self, execution: _TurnExecution):
        """注入循环的暂停函数：emit ask_user → 阻塞等答复（或 5 分钟超时空答复）。"""

        async def ask_user_fn(
            question: str,
            options: list[dict[str, str]],
            ask_id: str,
            *,
            allow_free_text: bool = False,
            context: str = "",
        ) -> str:
            execution.awaiting_user_reply = True
            execution.pending_ask_id = ask_id
            execution.reply_queue = asyncio.Queue()
            await execution.bus.emit_ask_user(
                question=question,
                options=options,
                ask_id=ask_id,
                allow_free_text=allow_free_text,
                context=context,
            )
            timeout_task: asyncio.Task | None = None
            if not execution.subscribers:
                # §7.20：无前端连接时 5 分钟超时自动空答复（防挂起）
                timeout_task = asyncio.create_task(self._ask_timeout(execution, ask_id))
                execution.reply_timeout_task = timeout_task
            try:
                reply_ask_id, answer = await execution.reply_queue.get()
                await execution.bus.emit_ask_user_reply(ask_id=reply_ask_id, answer=answer)
                return answer
            finally:
                if timeout_task is not None and not timeout_task.done():
                    timeout_task.cancel()
                execution.awaiting_user_reply = False
                execution.pending_ask_id = None
                execution.reply_queue = None
                execution.reply_timeout_task = None

        return ask_user_fn

    async def _ask_timeout(self, execution: _TurnExecution, ask_id: str) -> None:
        try:
            await asyncio.sleep(ASK_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        # 超时：空答复解除挂起（若已被真实答复解决则丢弃）
        if execution.awaiting_user_reply and execution.reply_queue is not None:
            logger.warning("ask_user 5 分钟无答复，空答复解除挂起 turn=%s", execution.turn_id)
            execution.reply_queue.put_nowait((ask_id, ""))

    def _cancel_ask_timeout_on_subscribe(self, execution: _TurnExecution) -> None:
        """订阅者到达 → 取消超时计时（用户回来作答了）。"""
        if execution.awaiting_user_reply and execution.reply_timeout_task is not None:
            if not execution.reply_timeout_task.done():
                execution.reply_timeout_task.cancel()
            execution.reply_timeout_task = None
