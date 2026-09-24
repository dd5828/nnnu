"""笔记本 REST（§7.15 极简版 / §9.1）：笔记本增删查 + 记录增删查。

§9.1 的表格给的是 `GET/POST/PATCH/DELETE /notebooks` + `/notebooks/{id}/records`
——本批不带 PATCH（记录一经写入不可改，要改删了重存），记在 STAGE_LOG 偏离清单。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.notebooks.service import NotebookError, NotebookService

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


@router.delete("/api/v1/notebooks/{notebook_id}/records/{record_id}")
async def delete_record(notebook_id: str, record_id: str, http_request: Request):
    if not await _service(http_request).delete_record(notebook_id, record_id):
        return _not_found(f"记录 {record_id} 不在笔记本 {notebook_id} 中")
    return {"deleted": record_id}
