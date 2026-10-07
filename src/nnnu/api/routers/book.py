"""Book REST（§7.14 / §9 表）：书库增删改查 + 目录生成/编辑 + 编译触发/暂停 +
页详情/块编辑/页聊/作答/进度点/导出。

本层照 co_writer 先例只做三件事：请求体封口（错误码 → §9.1 状态码/信封）、
LLM 失败映射（输出坏 502 / 不可用 503）、取服务。创建会**同步**跑一次 spine
生成（一次模型调用，前端要放宽超时）；编译是 202 + 任务 + 前端轮询 GET；
块重生成/换类型同步等（animation 例外：202 + 轮询页）；页聊天非流式。
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from nnnu.book.chat import BookChatOutputError
from nnnu.book.llm import BookOutputError
from nnnu.book.models import BookError
from nnnu.book.service import BookService
from nnnu.services.llm.errors import LLMError
from nnnu.services.settings.service import get_settings_service

router = APIRouter()

# BookError.code → HTTP 状态；没列的按 422 参数类
_STATUS_BY_CODE = {
    "not_found": 404,
    "invalid_title": 422,
    "invalid_sources": 422,
    "invalid_spine": 422,
    "invalid_payload": 422,
    "invalid_block_type": 422,
    "invalid_block": 422,
    "unknown_chapter": 422,
    "sources_unavailable": 422,
    "not_draft": 409,
    "compile_busy": 409,
    "chat_busy": 409,
}
_RECOVERABLE_CODES = {"not_draft", "compile_busy", "chat_busy", "sources_unavailable"}


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _book_error(exc: BookError) -> JSONResponse:
    """业务错误 → 信封：404 / 409（状态不允许）/ 其余 422。"""
    status = _STATUS_BY_CODE.get(exc.code, 422)
    return _error(status, exc.code, str(exc), recoverable=exc.code in _RECOVERABLE_CODES)


def _llm_error(exc: LLMError) -> JSONResponse:
    """LLM 失败的统一信封：输出洗不出来 502，其余（没配模型/调用失败）503，都可恢复。"""
    if isinstance(exc, (BookOutputError, BookChatOutputError)):
        return _error(502, "llm_output_invalid", str(exc), recoverable=True)
    return _error(503, "llm_unavailable", str(exc), recoverable=True)


def _service(http_request: Request) -> BookService:
    return http_request.app.state.book


class CreateBookRequest(BaseModel):
    title: str = ""
    sources: dict[str, Any] = Field(default_factory=dict)
    language: str = "zh"


class ChapterPatch(BaseModel):
    """目录编辑的一行：key 认章，title 是新名字；数组顺序即新顺序。"""

    key: str
    title: str


class UpdateBookRequest(BaseModel):
    """局部更新：None = 不改；chapters 给的是**全量有序列表**。"""

    title: str | None = None
    chapters: list[ChapterPatch] | None = None


class CompileRequest(BaseModel):
    """编译触发：不带给整本（续跑）；给 chapters = 只整章重跑这几个 key。"""

    chapters: list[str] | None = None


class BlockEditRequest(BaseModel):
    """块编辑：六种 op 共用一封口，各 op 用得到的字段见服务层校验。"""

    op: str
    block_id: str | None = None
    type: str | None = None
    payload: dict[str, Any] | None = None
    focus: str | None = None
    direction: str | None = None  # move：up | down
    after_block_id: str | None = None  # insert：插到这个块后面（不带给追加到末尾）


class PageFlagsRequest(BaseModel):
    visited: bool | None = None
    bookmarked: bool | None = None


class ChatRequest(BaseModel):
    message: str


class AttemptRequest(BaseModel):
    block_id: str
    answer: list[str] = Field(default_factory=list)


@router.get("/api/v1/books")
async def list_books(http_request: Request):
    return {"books": await _service(http_request).list_books()}


@router.post("/api/v1/books")
async def create_book(http_request: Request, body: CreateBookRequest):
    service = _service(http_request)
    try:
        book = await service.create_book(
            title=body.title, sources=body.sources, language=body.language
        )
    except BookError as exc:
        return _book_error(exc)
    except LLMError as exc:
        return _llm_error(exc)
    detail = await service.book_detail(book.id)
    return detail if detail is not None else book.model_dump()


@router.get("/api/v1/books/{book_id}")
async def get_book(http_request: Request, book_id: str):
    detail = await _service(http_request).book_detail(book_id)
    if detail is None:
        return _not_found("书不存在")
    return detail


@router.patch("/api/v1/books/{book_id}")
async def update_book(http_request: Request, book_id: str, body: UpdateBookRequest):
    if body.title is None and body.chapters is None:
        return _error(422, "invalid_request", "没有要改的字段")
    service = _service(http_request)
    chapters = (
        None
        if body.chapters is None
        else [{"key": item.key, "title": item.title} for item in body.chapters]
    )
    try:
        await service.update_book(book_id, title=body.title, chapters=chapters)
    except BookError as exc:
        return _book_error(exc)
    detail = await service.book_detail(book_id)
    return detail if detail is not None else _not_found("书不存在")


@router.delete("/api/v1/books/{book_id}")
async def delete_book(http_request: Request, book_id: str):
    try:
        deleted = await _service(http_request).delete_book(book_id)
    except BookError as exc:
        return _book_error(exc)
    if not deleted:
        return _not_found("书不存在")
    return {"deleted": book_id}


@router.post("/api/v1/books/{book_id}/compile")
async def compile_book(http_request: Request, book_id: str, body: CompileRequest | None = None):
    """开编 / 续跑（202 立即返回，任务后台跑，前端轮询 GET /books/{id}）。"""
    try:
        await _service(http_request).compile_book(
            book_id, chapters=body.chapters if body is not None else None
        )
    except BookError as exc:
        return _book_error(exc)
    return JSONResponse(status_code=202, content={"started": True})


@router.post("/api/v1/books/{book_id}/compile/pause")
async def pause_compile(http_request: Request, book_id: str):
    """请求暂停：任务在下一个块边界收手落 paused；没任务在跑就是 no-op。"""
    try:
        paused = await _service(http_request).pause_compile(book_id)
    except BookError as exc:
        return _book_error(exc)
    return {"paused": paused}


@router.get("/api/v1/books/{book_id}/pages/{page_id}")
async def get_page(http_request: Request, book_id: str, page_id: str):
    """整页（阅读器用）：块含 payload + 本页聊天历史。"""
    detail = await _service(http_request).get_page_detail(book_id, page_id)
    if detail is None:
        return _not_found("页不存在")
    return detail


@router.patch("/api/v1/books/{book_id}/pages/{page_id}")
async def update_page_flags(
    http_request: Request, book_id: str, page_id: str, body: PageFlagsRequest
):
    """阅读进度点：visited / bookmarked。"""
    try:
        return await _service(http_request).set_page_flags(
            book_id, page_id, visited=body.visited, bookmarked=body.bookmarked
        )
    except BookError as exc:
        return _book_error(exc)


@router.patch("/api/v1/books/{book_id}/pages/{page_id}/blocks")
async def edit_blocks(http_request: Request, book_id: str, page_id: str, body: BlockEditRequest):
    """块编辑：move/update/delete/insert 立即返回块数组；regenerate/retype 同步生成
    （animation 例外：202 + 轮询页详情）。编译中 409。"""
    try:
        result = await _service(http_request).edit_blocks(
            book_id,
            page_id,
            op=body.op,
            block_id=body.block_id,
            block_type=body.type,
            payload=body.payload,
            focus=body.focus,
            direction=body.direction,
            after_block_id=body.after_block_id,
        )
    except BookError as exc:
        return _book_error(exc)
    except LLMError as exc:
        return _llm_error(exc)
    if result.get("started"):
        return JSONResponse(status_code=202, content=result)
    return result


@router.post("/api/v1/books/{book_id}/pages/{page_id}/chat")
async def chat_page(http_request: Request, book_id: str, page_id: str, body: ChatRequest):
    """页聊天（非流式）：答完一次性返回，引用出处直接取循环收集到的 citations。"""
    try:
        return await _service(http_request).chat_page(book_id, page_id, body.message)
    except BookError as exc:
        return _book_error(exc)
    except LLMError as exc:
        return _llm_error(exc)


@router.post("/api/v1/books/{book_id}/pages/{page_id}/attempts")
async def answer_attempt(http_request: Request, book_id: str, page_id: str, body: AttemptRequest):
    """测验作答：零 LLM 判分，落库并回对错与解析。"""
    try:
        return await _service(http_request).answer_attempt(
            book_id, page_id, block_id=body.block_id, answer=body.answer
        )
    except BookError as exc:
        return _book_error(exc)


@router.get("/api/v1/books/{book_id}/export")
async def export_book(http_request: Request, book_id: str):
    """整书 Markdown（语言随界面设置，照笔记本导出口径）。"""
    lang = str(get_settings_service().load_area("appearance").get("ui_language") or "zh")
    markdown = await _service(http_request).export_book(book_id, language=lang)
    if markdown is None:
        return _not_found("书不存在")
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{book_id}.md"'},
    )
