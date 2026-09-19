"""会话 REST（§9.1）：列表/新建/详情/重命名/删除/重新生成。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.runtime.orchestrator import TurnBusyError, TurnRejected
from nnnu.services.sessions.models import Session

router = APIRouter()


class SessionCreate(BaseModel):
    capability: str = "chat"
    language: str = "zh"


class RenameRequest(BaseModel):
    title: str


def _error(code: str, message: str, recoverable: bool, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _session_dict(session: Session) -> dict:
    return session.model_dump()


@router.get("/api/v1/sessions")
async def list_sessions(http_request: Request, limit: int = 50, offset: int = 0):
    sessions = await http_request.app.state.runtime._sessions.list_sessions(
        limit=limit, offset=offset
    )
    return {"sessions": [_session_dict(s) for s in sessions]}


@router.post("/api/v1/sessions")
async def create_session(body: SessionCreate, http_request: Request):
    session = await http_request.app.state.runtime._sessions.ensure_session(
        None, capability=body.capability, language=body.language
    )
    return _session_dict(session)


@router.get("/api/v1/sessions/{session_id}")
async def get_session(session_id: str, http_request: Request):
    sessions = http_request.app.state.runtime._sessions
    session = await sessions.get_session(session_id)
    if session is None:
        return _error("not_found", f"会话 {session_id} 不存在", False, 404)
    messages = await sessions.list_messages(session_id)
    return {**_session_dict(session), "messages": [m.model_dump() for m in messages]}


@router.patch("/api/v1/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameRequest, http_request: Request):
    session = await http_request.app.state.runtime._sessions.rename_session(session_id, body.title)
    if session is None:
        return _error("not_found", f"会话 {session_id} 不存在", False, 404)
    return _session_dict(session)


@router.delete("/api/v1/sessions/{session_id}")
async def delete_session(session_id: str, http_request: Request):
    await http_request.app.state.runtime._sessions.delete_session(session_id)
    return {"deleted": session_id}


@router.post("/api/v1/sessions/{session_id}/regenerate")
async def regenerate_session(session_id: str, http_request: Request):
    runtime = http_request.app.state.runtime
    try:
        return await runtime.regenerate(session_id)
    except TurnBusyError as exc:
        return _error("session_busy", str(exc), True, 409)
    except TurnRejected as exc:
        return _error("turn_rejected", str(exc), False, 400)
