"""笔记本 REST（§7.15 正式版 / §9.1）：笔记本增删改查 + 记录增删改移拷 + 导出。

§9.1 的 PATCH 偏离在 P9 正式版关闭：PATCH 记录合并「编辑 + 移动」（body 带
target_notebook_id 且不同即移动，记录 id 不变——@ 引用不随移动失效的根）。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from nnnu.services.notebooks.export import export_notebook_markdown
from nnnu.services.notebooks.service import NotebookError, NotebookService
from nnnu.services.settings.service import get_settings_service

router = APIRouter()

MAX_RECORDS_PER_QUERY = 200


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _service(http_request: Request) -> NotebookService:
    return http_request.app.state.notebooks


class CreateNotebookRequest(BaseModel):
    name: str
    description: str | None = None


class CreateRecordRequest(BaseModel):
    type: str = "note"
    title: str = ""
    content_md: str
    source_ref: str | None = None


class UpdateNotebookRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class UpdateRecordRequest(BaseModel):
    title: str | None = None
    content_md: str | None = None
    target_notebook_id: str | None = None


class CopyRecordRequest(BaseModel):
    target_notebook_id: str | None = None


@router.get("/api/v1/notebooks")
async def list_notebooks(http_request: Request):
    """笔记本列表。带上各本记录条数——控制台卡片要显示「N 条记录」。"""
    service = _service(http_request)
    notebooks = await service.list_notebooks()
    items = []
    for notebook in notebooks:
        records = await service.list_records(notebook.id)
        items.append({**notebook.model_dump(), "record_count": len(records)})
    return {"notebooks": items}


@router.post("/api/v1/notebooks")
async def create_notebook(body: CreateNotebookRequest, http_request: Request):
    try:
        notebook = await _service(http_request).create_notebook(body.name, body.description)
    except NotebookError as exc:
        return _error(422, "invalid_name", str(exc))
    return notebook.model_dump()


@router.get("/api/v1/notebooks/records/{record_id}")
async def get_record(record_id: str, http_request: Request):
    """全局按 id 查记录（不限定笔记本）：引用 chip 的深链跟随——记录移动后仍指对地方。"""
    service = _service(http_request)
    record = await service.get_record(record_id)
    if record is None:
        return _not_found(f"记录 {record_id} 不存在")
    notebook = await service.get_notebook(record.notebook_id)
    return {**record.model_dump(), "notebook_name": notebook.name if notebook else ""}


@router.get("/api/v1/notebooks/{notebook_id}")
async def get_notebook(notebook_id: str, http_request: Request):
    """笔记本详情：一次带回记录（批一不做分页，单本记录量级远小于上限）。"""
    service = _service(http_request)
    notebook = await service.get_notebook(notebook_id)
    if notebook is None:
        return _not_found(f"笔记本 {notebook_id} 不存在")
    records = await service.list_records(notebook_id)
    return {
        **notebook.model_dump(),
        "records": [record.model_dump() for record in records[:MAX_RECORDS_PER_QUERY]],
    }


@router.patch("/api/v1/notebooks/{notebook_id}")
async def update_notebook(notebook_id: str, body: UpdateNotebookRequest, http_request: Request):
    try:
        notebook = await _service(http_request).update_notebook(
            notebook_id, name=body.name, description=body.description
        )
    except NotebookError as exc:
        return _error(422, "invalid_name", str(exc))
    if notebook is None:
        return _not_found(f"笔记本 {notebook_id} 不存在")
    return notebook.model_dump()


@router.delete("/api/v1/notebooks/{notebook_id}")
async def delete_notebook(notebook_id: str, http_request: Request):
    if not await _service(http_request).delete_notebook(notebook_id):
        return _not_found(f"笔记本 {notebook_id} 不存在")
    return {"deleted": notebook_id}


@router.post("/api/v1/notebooks/{notebook_id}/records")
async def create_record(notebook_id: str, body: CreateRecordRequest, http_request: Request):
    service = _service(http_request)
    if await service.get_notebook(notebook_id) is None:
        return _not_found(f"笔记本 {notebook_id} 不存在")
    try:
        record = await service.add_record(
            notebook_id,
            record_type=body.type,
            title=body.title,
            content_md=body.content_md,
            source_ref=body.source_ref,
        )
    except NotebookError as exc:
        return _error(422, "invalid_record", str(exc))
    return record.model_dump()


@router.patch("/api/v1/notebooks/{notebook_id}/records/{record_id}")
async def update_record(
    notebook_id: str, record_id: str, body: UpdateRecordRequest, http_request: Request
):
    """编辑记录（title/content_md）；body 带不同 target_notebook_id 时同一请求先移动再编辑。"""
    service = _service(http_request)
    record = await service.get_record(record_id)
    if record is None or record.notebook_id != notebook_id:
        return _not_found(f"记录 {record_id} 不在笔记本 {notebook_id} 中")
    move_needed = bool(body.target_notebook_id) and body.target_notebook_id != notebook_id
    edit_needed = body.title is not None or body.content_md is not None
    if not move_needed and not edit_needed:
        return _error(422, "invalid_record", "没有要更新的字段")
    if move_needed:
        target = body.target_notebook_id or ""
        if await service.get_notebook(target) is None:
            return _not_found(f"笔记本 {target} 不存在")
        try:
            moved = await service.move_record(record_id, target)
        except NotebookError as exc:  # 预检后目标本又被删的窄竞态
            return _error(422, "invalid_record", str(exc))
        if moved is None:  # 本函数开头查到、这里没了（并发删除的窄竞态）
            return _not_found(f"记录 {record_id} 不存在")
        record = moved
    if edit_needed:
        try:
            updated = await service.update_record(
                record_id, title=body.title, content_md=body.content_md
            )
        except NotebookError as exc:
            return _error(422, "invalid_record", str(exc))
        if updated is not None:
            record = updated
    return record.model_dump()


@router.post("/api/v1/notebooks/{notebook_id}/records/{record_id}/copy")
async def copy_record(
    notebook_id: str,
    record_id: str,
    http_request: Request,
    body: CopyRecordRequest | None = None,
):
    service = _service(http_request)
    record = await service.get_record(record_id)
    if record is None or record.notebook_id != notebook_id:
        return _not_found(f"记录 {record_id} 不在笔记本 {notebook_id} 中")
    target = (body.target_notebook_id if body else None) or notebook_id
    if await service.get_notebook(target) is None:
        return _not_found(f"笔记本 {target} 不存在")
    try:
        copied = await service.copy_record(record_id, target_notebook_id=target)
    except NotebookError as exc:
        return _error(422, "invalid_record", str(exc))
    if copied is None:
        return _not_found(f"记录 {record_id} 不存在")
    return copied.model_dump()


@router.get("/api/v1/notebooks/{notebook_id}/export")
async def export_notebook(notebook_id: str, http_request: Request):
    """§7.15 笔记本导出：整本 Markdown（按记录创建时间升序）。"""
    service = _service(http_request)
    notebook = await service.get_notebook(notebook_id)
    if notebook is None:
        return _not_found(f"笔记本 {notebook_id} 不存在")
    records = await service.list_records(notebook_id)
    lang = str(get_settings_service().load_area("appearance").get("ui_language") or "zh")
    markdown = export_notebook_markdown(notebook, records, lang)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{notebook_id}.md"'},
    )


@router.delete("/api/v1/notebooks/{notebook_id}/records/{record_id}")
async def delete_record(notebook_id: str, record_id: str, http_request: Request):
    if not await _service(http_request).delete_record(notebook_id, record_id):
        return _not_found(f"记录 {record_id} 不在笔记本 {notebook_id} 中")
    return {"deleted": record_id}
