"""REST 兜底（§9.1）：POST /api/v1/chat——非流式单回合，内部走编排器。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nnnu.core.events import StreamEventType
from nnnu.runtime.orchestrator import TurnBusyError, TurnRejected
from nnnu.runtime.turn_runtime import TurnRequest

router = APIRouter()


@router.post("/api/v1/chat")
async def chat(request: TurnRequest, http_request: Request) -> JSONResponse:
    runtime = http_request.app.state.runtime
    try:
        started = await runtime.start_turn(request)
    except TurnBusyError as exc:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "session_busy", "message": str(exc), "recoverable": True}},
        )
    except TurnRejected as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "turn_rejected", "message": str(exc), "recoverable": False}},
        )
    events = []
    async for event in runtime.subscribe_turn(started["turn_id"]):
        events.append(event)
    done = next((e for e in reversed(events) if e["type"] == StreamEventType.DONE.value), None)
    cost = next(
        (e for e in reversed(events) if e["type"] == StreamEventType.COST_SUMMARY.value), None
    )
    return JSONResponse(
        {
            "turn_id": started["turn_id"],
            "session_id": events[0]["session_id"] if events else None,
            "status": done["payload"].get("status", "completed") if done else "failed",
            "response": done["payload"].get("response", "") if done else "",
            "tool_calls": done["payload"].get("tool_calls", []) if done else [],
            "citations": done["payload"].get("citations", []) if done else [],
            "cost_summary": cost["payload"] if cost else None,
        }
    )
