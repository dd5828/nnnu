"""Co-Writer REST（§7.13 / §9 表）：文档增删改查 + 改写发起/确认/拒绝。

编辑管线见 co_writer/edit.py（提示词+Agent 循环）与 co_writer/service.py（编排、
锁与 digest）。本层只做三件事：请求体封口（错误码 → §9.1 状态码/信封）、
LLM 失败映射（输出坏 502 / 不可用 503，照题库先例）、记账——改写用量按合成
turn_id `cowrite-<edit_id>` 写 usage_records（§6.9 未定义这条通路，同判分/分类
的偏离先例：不建会话、不写消息，只把成本挂上）。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nnnu.co_writer.edit import CoWriterOutputError
from nnnu.co_writer.models import CoWriterError
from nnnu.co_writer.service import CoWriterService
from nnnu.services.llm.errors import LLMError

router = APIRouter()

# CoWriterError.code → HTTP 状态（doc_changed/edit_expired 都是「重来一次」类冲突）
_STATUS_BY_CODE = {
    "invalid_doc_id": 404,
    "not_found": 404,
    "doc_changed": 409,
    "edit_expired": 409,
}
_RECOVERABLE_CODES = {"doc_changed", "edit_expired"}


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _co_writer_error(exc: CoWriterError) -> JSONResponse:
    """业务错误 → 信封：404 找不到 / 409 内容或待确认编辑已失效 / 其余 422 参数。"""
    status = _STATUS_BY_CODE.get(exc.code, 422)
    return _error(status, exc.code, str(exc), recoverable=exc.code in _RECOVERABLE_CODES)


def _llm_error(exc: LLMError) -> JSONResponse:
    """LLM 失败的统一信封：输出洗不出来 502，其余（没配模型/调用失败）503，都可恢复。"""
    if isinstance(exc, CoWriterOutputError):
        return _error(502, "llm_output_invalid", str(exc), recoverable=True)
    return _error(503, "llm_unavailable", str(exc), recoverable=True)


def _service(http_request: Request) -> CoWriterService:
    return http_request.app.state.co_writer


class CreateDocRequest(BaseModel):
    title: str = ""
    content: str = ""


class UpdateDocRequest(BaseModel):
    """局部更新：None = 不改；显式空串 = 清空（标题清空则回落到正文首行）。"""

    title: str | None = None
    content: str | None = None


class EditRequest(BaseModel):
    start: int
    end: int
    original: str
    action: str
    instruction: str = ""
    language: str = "zh"
    kb_ids: list[str] = Field(default_factory=list)
    use_web: bool = False


class ResolveEditRequest(BaseModel):
    action: str  # accept | reject


@router.get("/api/v1/co-writer")
async def list_docs(http_request: Request):
    return {"docs": await _service(http_request).list_docs()}


@router.post("/api/v1/co-writer")
async def create_doc(body: CreateDocRequest, http_request: Request):
    try:
        doc = await _service(http_request).create_doc(title=body.title, content=body.content)
    except CoWriterError as exc:
        return _co_writer_error(exc)
    return doc.model_dump()


@router.get("/api/v1/co-writer/{doc_id}")
async def get_doc(doc_id: str, http_request: Request):
    found = await _service(http_request).get_doc(doc_id)
    if found is None:
        return _not_found(f"文档 {doc_id} 不存在")
    meta, content = found
    return {**meta.model_dump(), "content": content}


@router.patch("/api/v1/co-writer/{doc_id}")
async def update_doc(doc_id: str, body: UpdateDocRequest, http_request: Request):
    """自动保存/重命名走这里；响应只回 meta 不回正文（正文在本地编辑器里）。"""
    if body.title is None and body.content is None:
        return _error(422, "invalid_request", "没有要更新的字段")
    try:
        doc = await _service(http_request).update_doc(
            doc_id, title=body.title, content=body.content
        )
    except CoWriterError as exc:
        return _co_writer_error(exc)
    if doc is None:
        return _not_found(f"文档 {doc_id} 不存在")
    return doc.model_dump()


@router.delete("/api/v1/co-writer/{doc_id}")
async def delete_doc(doc_id: str, http_request: Request):
    if not await _service(http_request).delete_doc(doc_id):
        return _not_found(f"文档 {doc_id} 不存在")
    return {"deleted": doc_id}


@router.post("/api/v1/co-writer/{doc_id}/edit")
async def start_edit(doc_id: str, body: EditRequest, http_request: Request):
    """发起改写：服务端校验切片与发起时正文一致，模型结果先挂待确认（不改文档）。"""
    try:
        result = await _service(http_request).start_edit(
            doc_id,
            start=body.start,
            end=body.end,
            original=body.original,
            action=body.action,
            instruction=body.instruction,
            language=body.language,
            kb_ids=body.kb_ids,
            use_web=body.use_web,
        )
    except CoWriterError as exc:
        return _co_writer_error(exc)
    except LLMError as exc:
        return _llm_error(exc)
    if result is None:
        return _not_found(f"文档 {doc_id} 不存在")
    usage = result.get("usage") or {}
    if usage.get("tokens"):
        await http_request.app.state.cost.record_turn(
            session_id="", turn_id=f"cowrite-{result['edit_id']}", summary=usage
        )
    return result


@router.post("/api/v1/co-writer/{doc_id}/edit/{edit_id}")
async def resolve_edit(doc_id: str, edit_id: str, body: ResolveEditRequest, http_request: Request):
    """确认或拒绝：accept 在锁内复核 digest 后写回；同一条编辑只能消费一次。"""
    if body.action not in ("accept", "reject"):
        return _error(422, "invalid_action", f"未知操作 {body.action!r}，允许：accept、reject")
    try:
        result = await _service(http_request).resolve_edit(doc_id, edit_id, body.action)
    except CoWriterError as exc:
        return _co_writer_error(exc)
    if result is None:
        return _not_found(f"文档 {doc_id} 不存在")
    return result
