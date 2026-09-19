"""WebSocket 统一端点（§7.20）：所有实时交互的通道。

入站：chat / stop / regenerate / ask_user_reply / resume / ping；
出站：6.1 全量 StreamEvent 信封（传输层已补单调 seq），
     turn_start / done / error / stopped 作为回合边界。
服务质量：30s 心跳、每连接单发送任务（事件有序）、断开后回合后台继续、
resume 按 seq 补发（三源合流在 TurnRuntime.subscribe_turn/session）。
"""

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from nnnu.core.events import StreamEventType
from nnnu.core.ids import new_id
from nnnu.runtime.orchestrator import TurnBusyError, TurnRejected
from nnnu.runtime.turn_runtime import TurnRequest, TurnRuntimeManager

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_INTERVAL_S = 30.0  # §7.20 心跳


def _frame(
    type_value: str,
    *,
    payload: dict | None = None,
    turn_id: str = "",
    session_id: str | None = None,
    seq: int = 0,
) -> dict:
    """控制帧/兜底帧（不走回合事件缓冲）。"""
    return {
        "type": type_value,
        "payload": payload or {},
        "turn_id": turn_id,
        "session_id": session_id,
        "seq": seq,
        "event_id": new_id("evt"),
        "ts": time.time(),
    }


async def _heartbeat(send_queue: asyncio.Queue, closed: asyncio.Event) -> None:
    try:
        while not closed.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            if not closed.is_set():
                send_queue.put_nowait(_frame(StreamEventType.HEARTBEAT.value))
    except asyncio.CancelledError:
        pass


@router.websocket("/api/v1/ws")
async def unified_ws(ws: WebSocket) -> None:
    await ws.accept()
    runtime: TurnRuntimeManager = ws.app.state.runtime
    send_queue: asyncio.Queue[dict | None] = asyncio.Queue()
    closed = asyncio.Event()
    forwarders: list[asyncio.Task] = []

    async def sender() -> None:
        """每连接单发送任务：事件有序（§7.20）。"""
        while True:
            item = await send_queue.get()
            if item is None:
                return
            try:
                await ws.send_json(item)
            except Exception:
                logger.debug("WS 发送失败，连接已断")
                return

    def forward_turn(turn_id: str, after_seq: int = 0) -> None:
        async def _forward() -> None:
            try:
                async for event in runtime.subscribe_turn(turn_id, after_seq):
                    send_queue.put_nowait(event)
            except asyncio.CancelledError:
                pass

        forwarders.append(asyncio.create_task(_forward()))

    def forward_session(session_id: str, after_seq: int = 0) -> None:
        async def _forward() -> None:
            try:
                async for event in runtime.subscribe_session(session_id, after_seq):
                    send_queue.put_nowait(event)
            except asyncio.CancelledError:
                pass

        forwarders.append(asyncio.create_task(_forward()))

    sender_task = asyncio.create_task(sender())
    heartbeat_task = asyncio.create_task(_heartbeat(send_queue, closed))
    try:
        while True:
            raw = await ws.receive_json()
            msg_type = raw.get("type", "")
            try:
                if msg_type == "ping":
                    send_queue.put_nowait(_frame(StreamEventType.HEARTBEAT.value))
                elif msg_type == "chat":
                    request = TurnRequest(**{k: v for k, v in raw.items() if k != "type"})
                    started = await runtime.start_turn(request)
                    forward_turn(started["turn_id"])
                elif msg_type == "stop":
                    ok = await runtime.stop_turn(str(raw.get("turn_id", "")))
                    if not ok:
                        send_queue.put_nowait(
                            _frame(
                                StreamEventType.ERROR.value,
                                payload={"message": "回合不存在或已结束", "recoverable": False},
                            )
                        )
                elif msg_type == "regenerate":
                    started = await runtime.regenerate(str(raw["session_id"]))
                    forward_turn(started["turn_id"])
                elif msg_type == "ask_user_reply":
                    ok = await runtime.submit_user_reply(
                        str(raw.get("turn_id", "")),
                        str(raw.get("ask_id", "")),
                        str(raw.get("answer", "")),
                    )
                    if not ok:
                        send_queue.put_nowait(
                            _frame(
                                StreamEventType.ERROR.value,
                                payload={
                                    "message": "回合未在等待答复或 ask_id 不符",
                                    "recoverable": False,
                                },
                            )
                        )
                elif msg_type == "resume":
                    session_id = str(raw.get("session_id", ""))
                    after_seq = int(raw.get("after_seq", 0))
                    if session_id:
                        forward_session(session_id, after_seq)
                else:
                    send_queue.put_nowait(
                        _frame(
                            StreamEventType.ERROR.value,
                            payload={"message": f"未知消息类型 {msg_type}", "recoverable": False},
                        )
                    )
            except TurnBusyError as exc:
                send_queue.put_nowait(
                    _frame(
                        StreamEventType.ERROR.value,
                        payload={"message": str(exc), "recoverable": True},
                    )
                )
            except TurnRejected as exc:
                send_queue.put_nowait(
                    _frame(
                        StreamEventType.ERROR.value,
                        payload={"message": str(exc), "recoverable": False},
                    )
                )
            except Exception as exc:
                logger.exception("WS 消息处理异常")
                send_queue.put_nowait(
                    _frame(
                        StreamEventType.ERROR.value,
                        payload={"message": f"处理失败: {exc}", "recoverable": False},
                    )
                )
    except WebSocketDisconnect:
        logger.debug("WS 断开（回合后台继续）")
    except Exception:
        logger.exception("WS 异常")
    finally:
        closed.set()
        heartbeat_task.cancel()
        for task in forwarders:
            task.cancel()
        send_queue.put_nowait(None)
        await sender_task
