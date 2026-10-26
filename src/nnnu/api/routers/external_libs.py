"""外部库 REST（§7.9，P14）：Obsidian 原位读写、IMA 浏览/检索/写回、MarginNote 接收端。

三块共用 `/api/v1/libraries` 前缀，但协议各自独立：

- Obsidian：挂载 vault → 列目录 / 读笔记 / 写回（覆盖或追加）；路径越界一律 422。
- IMA：设置页配好凭证后，浏览知识库、检索、读正文、检索/新建/追加笔记、收录网页。
- MarginNote：配对设备拿一次性 token；Add-on 用 `Authorization: MarginNote <id>:<token>`
  推增量批次；其余端点是给界面浏览已接收的数据。

错误信封与知识库路由一致（§9.1）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nnnu.services.external_libs.ima import (
    ImaAuthError,
    ImaConfigError,
    ImaError,
    ImaRateLimitError,
    ImaService,
)
from nnnu.services.external_libs.marginnote import (
    MAX_BATCH_OBJECTS,
    MAX_LINKS,
    MAX_TAGS,
    MAX_TEXT_CHARS,
    MarginNoteAuthError,
    MarginNoteError,
    MarginNoteService,
    MnObject,
)
from nnnu.services.external_libs.obsidian import (
    MAX_NOTE_BYTES,
    NoteNotFound,
    ObsidianService,
    VaultError,
    VaultNotFound,
)

router = APIRouter()


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _obsidian(request: Request) -> ObsidianService:
    return request.app.state.obsidian


def _ima(request: Request) -> ImaService:
    return request.app.state.ima


def _marginnote(request: Request) -> MarginNoteService:
    return request.app.state.marginnote


# ================= 总览 =================


@router.get("/api/v1/libraries")
async def libraries_overview(http_request: Request):
    obsidian: ObsidianService = _obsidian(http_request)
    ima: ImaService = _ima(http_request)
    marginnote: MarginNoteService = _marginnote(http_request)
    return {
        "obsidian": {"vaults": [vault.to_dict() for vault in obsidian.list_vaults()]},
        "ima": ima.status(),
        "marginnote": marginnote.status(),
    }


# ================= Obsidian =================


class AddVaultRequest(BaseModel):
    path: str
    name: str = Field("", max_length=120)


class WriteNoteRequest(BaseModel):
    path: str
    content: str = Field(..., max_length=MAX_NOTE_BYTES)
    mode: str = Field("overwrite", pattern="^(overwrite|append)$")


def _vault_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, VaultNotFound):
        return _error(404, "not_found", str(exc))
    if isinstance(exc, NoteNotFound):
        return _error(404, "note_not_found", str(exc))
    return _error(422, "invalid_vault", str(exc))


@router.get("/api/v1/libraries/obsidian/vaults")
async def list_vaults(http_request: Request):
    return {"vaults": [vault.to_dict() for vault in _obsidian(http_request).list_vaults()]}


@router.post("/api/v1/libraries/obsidian/vaults")
async def add_vault(body: AddVaultRequest, http_request: Request):
    try:
        vault = await _obsidian(http_request).add_vault(body.path, name=body.name)
    except VaultError as exc:
        return _vault_error(exc)
    return {"vault": vault.to_dict()}


@router.delete("/api/v1/libraries/obsidian/vaults/{vault_id}")
async def remove_vault(vault_id: str, http_request: Request):
    try:
        vault = await _obsidian(http_request).remove_vault(vault_id)
    except VaultError as exc:
        return _vault_error(exc)
    return {"removed": vault.vault_id}


@router.get("/api/v1/libraries/obsidian/vaults/{vault_id}/notes")
async def list_notes(
    vault_id: str,
    http_request: Request,
    folder: str = "",
    limit: int = Query(200, ge=1, le=1000),
):
    try:
        return await _obsidian(http_request).list_notes(vault_id, folder=folder, limit=limit)
    except VaultError as exc:
        return _vault_error(exc)


@router.get("/api/v1/libraries/obsidian/vaults/{vault_id}/search")
async def search_notes(
    vault_id: str,
    http_request: Request,
    q: str = "",
    limit: int = Query(20, ge=1, le=50),
):
    if not q.strip():
        return _error(422, "invalid_query", "检索词不能为空")
    try:
        return await _obsidian(http_request).search(vault_id, q, limit=limit)
    except VaultError as exc:
        return _vault_error(exc)


@router.get("/api/v1/libraries/obsidian/vaults/{vault_id}/note")
async def read_note(vault_id: str, http_request: Request, path: str = ""):
    try:
        return await _obsidian(http_request).read_note(vault_id, path)
    except VaultError as exc:
        return _vault_error(exc)


@router.put("/api/v1/libraries/obsidian/vaults/{vault_id}/note")
async def write_note(vault_id: str, body: WriteNoteRequest, http_request: Request):
    try:
        return await _obsidian(http_request).write_note(
            vault_id, body.path, body.content, mode=body.mode
        )
    except VaultError as exc:
        return _vault_error(exc)


# ================= Tencent IMA =================


class ImaWriteNoteRequest(BaseModel):
    content: str = Field(..., max_length=200_000)
    folder_id: str = ""


class ImaAppendNoteRequest(BaseModel):
    content: str = Field(..., max_length=200_000)


class ImaUrlsRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=10)
    folder_id: str = ""


def _ima_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, ImaConfigError):
        return _error(422, "ima_not_configured", str(exc))
    if isinstance(exc, ImaAuthError):
        return _error(502, "ima_auth", str(exc))
    if isinstance(exc, ImaRateLimitError):
        return _error(429, "ima_rate_limited", str(exc), recoverable=True)
    if isinstance(exc, ImaError):
        return _error(502, "ima_error", str(exc), recoverable=True)
    return _error(422, "invalid_request", str(exc))


@router.get("/api/v1/libraries/ima/status")
async def ima_status(http_request: Request):
    return _ima(http_request).status()


@router.post("/api/v1/libraries/ima/test")
async def ima_test(http_request: Request):
    return await _ima(http_request).test_connection()


@router.get("/api/v1/libraries/ima/browse")
async def ima_browse(
    http_request: Request,
    folder_id: str = "",
    cursor: str = "",
    limit: int = Query(50, ge=1, le=50),
):
    try:
        return await _ima(http_request).browse(folder_id=folder_id, cursor=cursor, limit=limit)
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


@router.get("/api/v1/libraries/ima/search")
async def ima_search(http_request: Request, q: str = "", limit: int = Query(10, ge=1, le=50)):
    if not q.strip():
        return _error(422, "invalid_query", "检索词不能为空")
    try:
        return await _ima(http_request).search(q, limit=limit)
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


@router.get("/api/v1/libraries/ima/items/{media_id}")
async def ima_read_item(media_id: str, http_request: Request):
    try:
        return await _ima(http_request).read_item(media_id)
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


@router.get("/api/v1/libraries/ima/notes")
async def ima_search_notes(
    http_request: Request,
    q: str = "",
    by_content: bool = False,
    sort: str = "updated",
    limit: int = Query(20, ge=1, le=50),
):
    try:
        return await _ima(http_request).search_notes(
            q, by_content=by_content, sort=sort, limit=limit
        )
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


@router.post("/api/v1/libraries/ima/notes")
async def ima_create_note(body: ImaWriteNoteRequest, http_request: Request):
    try:
        return await _ima(http_request).create_note(body.content, folder_id=body.folder_id)
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


@router.post("/api/v1/libraries/ima/notes/{note_id}/append")
async def ima_append_note(note_id: str, body: ImaAppendNoteRequest, http_request: Request):
    try:
        return await _ima(http_request).append_note(note_id, body.content)
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


@router.post("/api/v1/libraries/ima/urls")
async def ima_add_urls(body: ImaUrlsRequest, http_request: Request):
    try:
        return await _ima(http_request).add_urls(body.urls, folder_id=body.folder_id)
    except (ImaConfigError, ImaError, ValueError) as exc:
        return _ima_error(exc)


# ================= MarginNote 4 =================


class PairRequest(BaseModel):
    device_name: str = Field("", max_length=128)
    device_kind: str = Field("macos", max_length=32)


class SyncObjectIn(BaseModel):
    object_id: str = Field(..., max_length=200)
    object_type: str = Field(..., max_length=32)
    title: str = Field("", max_length=MAX_TEXT_CHARS)
    content: str = Field("", max_length=MAX_TEXT_CHARS)
    excerpt: str | None = Field(None, max_length=MAX_TEXT_CHARS)
    document_id: str | None = Field(None, max_length=200)
    document_title: str | None = Field(None, max_length=500)
    page: int | None = None
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    links: list[str] = Field(default_factory=list, max_length=MAX_LINKS)
    color: str | None = Field(None, max_length=32)
    created_at: str = Field("", max_length=64)
    updated_at: str = Field("", max_length=64)
    raw: dict[str, Any] = Field(default_factory=dict)


class SyncRequest(BaseModel):
    cursor: str = Field("", max_length=256)
    objects: list[SyncObjectIn] = Field(default_factory=list, max_length=MAX_BATCH_OBJECTS)
    deleted_ids: list[str] = Field(default_factory=list, max_length=MAX_BATCH_OBJECTS)


def _device_from_header(
    service: MarginNoteService, authorization: str | None
) -> tuple[str, JSONResponse | None]:
    if not authorization or not authorization.startswith("MarginNote "):
        return "", _error(401, "missing_token", "缺少 MarginNote 设备令牌")
    raw = authorization[len("MarginNote ") :]
    if ":" not in raw:
        return "", _error(401, "bad_token", "设备令牌格式应为 <device_id>:<token>")
    device_id, token = raw.split(":", 1)
    if not service.verify_device(device_id, token):
        return "", _error(403, "invalid_token", "设备令牌无效或已停用")
    return device_id, None


@router.post("/api/v1/libraries/marginnote/pair")
async def marginnote_pair(body: PairRequest, http_request: Request):
    device, token = await _marginnote(http_request).pair_device(
        device_name=body.device_name, device_kind=body.device_kind
    )
    return {"device": device, "token": token}


@router.get("/api/v1/libraries/marginnote/devices")
async def marginnote_devices(http_request: Request):
    return {"devices": _marginnote(http_request).list_devices()}


@router.delete("/api/v1/libraries/marginnote/devices/{device_id}")
async def marginnote_revoke(device_id: str, http_request: Request):
    try:
        device = await _marginnote(http_request).revoke_device(device_id)
    except MarginNoteError as exc:
        return _error(404, "not_found", str(exc))
    return {"device": device}


@router.get("/api/v1/libraries/marginnote/status")
async def marginnote_status(http_request: Request):
    return _marginnote(http_request).status()


@router.post("/api/v1/libraries/marginnote/sync")
async def marginnote_sync(
    body: SyncRequest,
    http_request: Request,
    authorization: str | None = Header(None),
):
    service = _marginnote(http_request)
    device_id, problem = _device_from_header(service, authorization)
    if problem is not None:
        return problem
    objects = [
        MnObject(
            object_id=row.object_id,
            object_type=row.object_type,
            title=row.title,
            content=row.content,
            excerpt=row.excerpt,
            document_id=row.document_id,
            document_title=row.document_title,
            page=row.page,
            tags=list(row.tags),
            links=list(row.links),
            color=row.color,
            created_at=row.created_at,
            updated_at=row.updated_at,
            raw=row.raw,
        )
        for row in body.objects
    ]
    try:
        result = await service.ingest(
            device_id,
            cursor=body.cursor,
            objects=objects,
            deleted_ids=list(body.deleted_ids),
        )
    except MarginNoteAuthError as exc:
        return _error(403, "invalid_token", str(exc))
    except MarginNoteError as exc:
        return _error(422, "invalid_sync", str(exc))
    return result


@router.get("/api/v1/libraries/marginnote/objects")
async def marginnote_objects(
    http_request: Request,
    q: str = "",
    object_type: str = "",
    document_id: str = "",
    limit: int = Query(200, ge=1, le=1000),
):
    service = _marginnote(http_request)
    if q.strip():
        rows = service.search(q, object_type=object_type, limit=limit)
        return {"query": q, "count": len(rows), "results": rows}
    rows = service.list_objects(object_type=object_type, document_id=document_id, limit=limit)
    return {"count": len(rows), "objects": rows}


@router.get("/api/v1/libraries/marginnote/objects/{object_id}")
async def marginnote_object(object_id: str, http_request: Request):
    row = _marginnote(http_request).get(object_id)
    if row is None:
        return _error(404, "not_found", f"对象不存在：{object_id}")
    return row


@router.get("/api/v1/libraries/marginnote/objects/{object_id}/links")
async def marginnote_links(object_id: str, http_request: Request):
    try:
        rows = _marginnote(http_request).linked_objects(object_id)
    except MarginNoteError as exc:
        return _error(404, "not_found", str(exc))
    return {"object_id": object_id, "count": len(rows), "links": rows}


@router.get("/api/v1/libraries/marginnote/documents")
async def marginnote_documents(http_request: Request):
    rows = _marginnote(http_request).documents()
    return {"count": len(rows), "documents": rows}


@router.get("/api/v1/libraries/marginnote/tags")
async def marginnote_tags(http_request: Request, limit: int = Query(200, ge=1, le=1000)):
    rows = _marginnote(http_request).tags(limit=limit)
    return {"count": len(rows), "tags": rows}
