"""附件 REST（§9.1）：上传（multipart）/ 下载 / 列表 / 删除（解析缓存清理入口）。

上传即解析（asyncio.to_thread）：PDF 页级、Office markitdown、文本直读；
MIME 白名单 + 扩展名回退（§11.3），大小与数量上限校验。
"""

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from nnnu.services.files import service as files_service

router = APIRouter()


def _error(code: str, message: str, recoverable: bool, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


@router.post("/api/v1/attachments")
async def upload_attachment(
    http_request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(...),
):
    if not session_id.strip():
        return _error("invalid_request", "session_id 不能为空", False, 422)
    content = await file.read()
    if len(content) > files_service.MAX_UPLOAD_BYTES:
        return _error(
            "attachment_too_large",
            f"附件超过大小上限（{files_service.MAX_UPLOAD_BYTES // (1024 * 1024)}MB）",
            False,
            422,
        )
    mime = files_service.normalize_mime(file.filename or "", file.content_type)
    if mime is None:
        return _error(
            "unsupported_mime",
            "不支持的文件类型（支持 PDF/Office/TXT/MD/CSV/代码/图片）",
            False,
            422,
        )
    service = http_request.app.state.attachments
    record = await service.save_upload(session_id, file.filename or "unnamed", content, mime)
    return record.model_dump()


@router.get("/api/v1/attachments")
async def list_attachments(http_request: Request, session_id: str = ""):
    if not session_id:
        return _error("invalid_request", "session_id 不能为空", False, 422)
    records = await http_request.app.state.attachments.list_for_session(session_id)
    return {"attachments": [record.model_dump() for record in records]}


@router.get("/api/v1/attachments/{attachment_id}")
async def download_attachment(attachment_id: str, http_request: Request):
    service = http_request.app.state.attachments
    record = await service.get(attachment_id)
    if record is None:
        return _error("not_found", f"附件 {attachment_id} 不存在", False, 404)
    path = service.resolve_path(record)
    if not path.is_file():
        return _error("file_missing", "附件文件缺失（可能已被清理）", False, 404)
    return FileResponse(path, filename=record.name, media_type=record.mime)


@router.delete("/api/v1/attachments/{attachment_id}")
async def delete_attachment(attachment_id: str, http_request: Request):
    deleted = await http_request.app.state.attachments.delete(attachment_id)
    if not deleted:
        return _error("not_found", f"附件 {attachment_id} 不存在", False, 404)
    return {"deleted": attachment_id}
