"""会话 REST（§9.1）：列表/新建/详情/重命名/删除/重新生成。

PATCH 支持会话级 persona（§7.1 粘性）：字段缺席即不变，显式 null 即清除。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, model_validator

from nnnu.capabilities.chat.personas import CUSTOM_PERSONA, is_valid_persona
from nnnu.runtime.orchestrator import TurnBusyError, TurnRejected
from nnnu.services.sessions.export import export_session_markdown
from nnnu.services.sessions.models import Session

router = APIRouter()


class SessionCreate(BaseModel):
    capability: str = "chat"
    language: str = "zh"


class SessionPatch(BaseModel):
    """PATCH /sessions/{id}：全部字段可选，只应用显式提供的字段。"""

    title: str | None = None
    persona: str | None = None  # 预设 id / custom / null（清除）
    persona_description: str | None = None

    @model_validator(mode="after")
    def _validate_persona(self) -> "SessionPatch":
        if "persona" in self.model_fields_set and self.persona is not None:
            if not is_valid_persona(self.persona):
                raise ValueError(f"未知 persona: {self.persona}")
            if self.persona == CUSTOM_PERSONA and not (self.persona_description or "").strip():
                raise ValueError("自定义 persona 需要 persona_description")
        return self


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
async def patch_session(session_id: str, body: SessionPatch, http_request: Request):
    sessions = http_request.app.state.runtime._sessions
    session = await sessions.get_session(session_id)
    if session is None:
        return _error("not_found", f"会话 {session_id} 不存在", False, 404)
    if "title" in body.model_fields_set and body.title is not None:
        session = await sessions.rename_session(session_id, body.title)
    if "persona" in body.model_fields_set:
        # 预设 persona 忽略（清空）描述；custom 用描述；null 清除两者
        description = body.persona_description if body.persona == CUSTOM_PERSONA else None
        session = await sessions.set_persona(session_id, body.persona, description)
    return _session_dict(session)


@router.delete("/api/v1/sessions/{session_id}")
async def delete_session(session_id: str, http_request: Request):
    await http_request.app.state.runtime._sessions.delete_session(session_id)
    return {"deleted": session_id}


@router.get("/api/v1/sessions/{session_id}/export")
async def export_session(session_id: str, http_request: Request):
    """§7.1 会话导出：完整 Markdown 转录（消息+思考+工具轨迹+引用+成本）。"""
    sessions = http_request.app.state.runtime._sessions
    session = await sessions.get_session(session_id)
    if session is None:
        return _error("not_found", f"会话 {session_id} 不存在", False, 404)
    messages = await sessions.list_messages(session_id)
    markdown = export_session_markdown(session, messages)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


@router.post("/api/v1/sessions/{session_id}/regenerate")
async def regenerate_session(session_id: str, http_request: Request):
    runtime = http_request.app.state.runtime
    try:
        return await runtime.regenerate(session_id)
    except TurnBusyError as exc:
        return _error("session_busy", str(exc), True, 409)
    except TurnRejected as exc:
        return _error("turn_rejected", str(exc), False, 400)
