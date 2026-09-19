"""流总线（§6.2）：per-turn 发布-订阅。

- publish：追加历史 → 各订阅队列 put_nowait（无界队列，永不阻塞发布方）；
- subscribe：async generator——历史截断与队列注册在同一同步步完成
  （防"边回放边 append 导致重复投递"），按 types 过滤，close 后投 None 哨兵；
- add_consumer：Consumer 协议适配，on_event 异常 try/except 隔离不影响主线；
- terminal_emitted：终局事件（done/error/stopped）每回合只接受一次，
  是编排器统一信封兜底（§6.5）的防双发依据；
- 模块级注册表：turn_id → bus（ask_user 回复 / stop 路由，§6.5）。

seq 不在 bus 层——传输层（TurnRuntime）缓冲时统一分配（§7.20 事件有序）。
"""

import asyncio
import logging
from typing import AsyncIterator, Protocol

from nnnu.core.events import TERMINAL_TYPES, StreamEvent, StreamEventType

logger = logging.getLogger(__name__)


class Consumer(Protocol):
    async def on_event(self, event: StreamEvent) -> None: ...


class StreamBus:
    def __init__(
        self, turn_id: str, *, session_id: str | None = None, max_history: int | None = None
    ) -> None:
        self.turn_id = turn_id
        self.session_id = session_id
        self._history: list[StreamEvent] = []
        self._max_history = max_history
        self._subscribers: set[asyncio.Queue[StreamEvent | None]] = set()
        self._consumer_tasks: list[asyncio.Task] = []
        self._closed = False
        self._terminal_emitted = False

    @property
    def terminal_emitted(self) -> bool:
        return self._terminal_emitted

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def history(self) -> list[StreamEvent]:
        """完整事件历史（传输层缓冲/测试断言用）。"""
        return list(self._history)

    async def publish(self, event: StreamEvent) -> None:
        if self._closed:
            logger.warning("bus %s 已关闭，丢弃事件 %s", self.turn_id, event.type)
            return
        if event.type in TERMINAL_TYPES:
            if self._terminal_emitted:
                logger.warning(
                    "bus %s 重复终局事件 %s，已忽略（信封防双发）", self.turn_id, event.type
                )
                return
            self._terminal_emitted = True
        self._history.append(event)
        if self._max_history is not None and len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]
        for queue in self._subscribers:
            queue.put_nowait(event)

    def subscribe(
        self, *, types: set[StreamEventType] | None = None, after: int = 0
    ) -> AsyncIterator[StreamEvent]:
        """订阅事件流：先同步回放 after 之后的历史，再续实时队列；close 后自然结束。

        after 为历史条数游标（skip 前 after 条）；注册与快照同同步步，无重复投递。
        """
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        replay = [e for e in self._history[after:] if types is None or e.type in types]
        registered = not self._closed
        if registered:
            self._subscribers.add(queue)

        async def _gen() -> AsyncIterator[StreamEvent]:
            try:
                for event in replay:
                    yield event
                if registered:
                    # 排空到 None 哨兵：注册后关闭时队列里可能还有尾事件
                    while True:
                        item = await queue.get()
                        if item is None:
                            return
                        if types is None or item.type in types:
                            yield item
            finally:
                self._subscribers.discard(queue)

        return _gen()

    def add_consumer(
        self, consumer: Consumer, *, types: set[StreamEventType] | None = None
    ) -> None:
        """Consumer 协议适配：转发任务挂后台，on_event 异常隔离（§6.2）。"""
        self._consumer_tasks = [t for t in self._consumer_tasks if not t.done()]

        async def _relay() -> None:
            async for event in self.subscribe(types=types):
                try:
                    await consumer.on_event(event)
                except Exception:
                    logger.exception("订阅者 on_event 异常已隔离（不影响主链路）")

        self._consumer_tasks.append(asyncio.create_task(_relay()))

    def mark_closed(self) -> None:
        """同步关闭（无界队列 put_nowait 不阻塞）：给所有订阅者投 None 哨兵。"""
        if self._closed:
            return
        self._closed = True
        for queue in self._subscribers:
            queue.put_nowait(None)
        self._subscribers.clear()

    # ---- 便捷 emit_*（§6.1 一一对应；session_id 由 bus 统一携带） ----

    async def emit(self, type: StreamEventType, **payload_kwargs) -> StreamEvent:
        event = StreamEvent.make(type, self.turn_id, session_id=self.session_id, **payload_kwargs)
        await self.publish(event)
        return event

    async def emit_turn_start(self, *, capability: str, model: str | None = None) -> StreamEvent:
        return await self.emit(StreamEventType.TURN_START, capability=capability, model=model)

    async def emit_status(self, *, stage: str, message: str = "") -> StreamEvent:
        return await self.emit(StreamEventType.STATUS, stage=stage, message=message)

    async def emit_content_delta(self, *, text: str) -> StreamEvent:
        return await self.emit(StreamEventType.CONTENT_DELTA, text=text)

    async def emit_content_done(self, *, full_text: str) -> StreamEvent:
        return await self.emit(StreamEventType.CONTENT_DONE, full_text=full_text)

    async def emit_thinking_delta(self, *, text: str) -> StreamEvent:
        return await self.emit(StreamEventType.THINKING_DELTA, text=text)

    async def emit_thinking_done(self, *, text: str) -> StreamEvent:
        return await self.emit(StreamEventType.THINKING_DONE, text=text)

    async def emit_tool_call(self, *, tool_name: str, args: dict, call_id: str) -> StreamEvent:
        return await self.emit(
            StreamEventType.TOOL_CALL, tool_name=tool_name, args=args, call_id=call_id
        )

    async def emit_tool_result(
        self,
        *,
        call_id: str,
        ok: bool,
        summary: str,
        detail: dict | None = None,
        usage: dict | None = None,
    ) -> StreamEvent:
        return await self.emit(
            StreamEventType.TOOL_RESULT,
            call_id=call_id,
            ok=ok,
            summary=summary,
            detail=detail,
            usage=usage,
        )

    async def emit_citation(self, *, sources: list[dict]) -> StreamEvent:
        return await self.emit(StreamEventType.CITATION, sources=sources)

    async def emit_ask_user(self, *, question: str, options: list[str], ask_id: str) -> StreamEvent:
        return await self.emit(
            StreamEventType.ASK_USER, question=question, options=options, ask_id=ask_id
        )

    async def emit_ask_user_reply(self, *, ask_id: str, answer: str) -> StreamEvent:
        return await self.emit(StreamEventType.ASK_USER_REPLY, ask_id=ask_id, answer=answer)

    async def emit_warning(self, *, message: str) -> StreamEvent:
        return await self.emit(StreamEventType.WARNING, message=message)

    async def emit_error(self, *, message: str, recoverable: bool = False) -> StreamEvent:
        return await self.emit(StreamEventType.ERROR, message=message, recoverable=recoverable)

    async def emit_cost_summary(self, *, tokens: int, cost: float, per_model: dict) -> StreamEvent:
        return await self.emit(
            StreamEventType.COST_SUMMARY, tokens=tokens, cost=cost, per_model=per_model
        )

    async def emit_done(self, *, response: str, **kwargs) -> StreamEvent:
        return await self.emit(StreamEventType.DONE, response=response, **kwargs)

    async def emit_stopped(self) -> StreamEvent:
        return await self.emit(StreamEventType.STOPPED)

    async def emit_heartbeat(self) -> StreamEvent:
        return await self.emit(StreamEventType.HEARTBEAT)


# ---- 模块级注册表：turn_id → bus（ask_user 回复 / stop 路由，§6.5） ----


_buses: dict[str, StreamBus] = {}


def register_bus(turn_id: str, bus: StreamBus) -> None:
    if turn_id in _buses:
        logger.warning("bus 注册表覆盖 turn_id=%s", turn_id)
    _buses[turn_id] = bus


def unregister_bus(turn_id: str) -> None:
    _buses.pop(turn_id, None)


def get_bus(turn_id: str) -> StreamBus | None:
    return _buses.get(turn_id)
