"""知识库 REST（§9.1）：列表/创建/删除、文档上传与进度、检索测试台、重建与取消。

补充端点（开发方案 §9.1 之外，记在 STAGE_LOG 偏离清单里）：
- `POST /kbs/{id}/build/cancel`：§7.9 要求构建可中断，没有对应端点就落不了地；
- `GET /kbs/{id}/docs/{doc_id}/file`：阅读器直接看原件（PDF 用 iframe + #page=N）；
- `GET /kbs/{id}/docs/{doc_id}/content`：文本类文档的解析结果，前端 <pre> 渲染。

`/sources`（GitHub 源 / 外部库绑定）P4 留空壳返回 501，P14 交付。
检索只用 mode（vector/hybrid）：P4 只有一个引擎，engine 参数到 P14 才有意义。
"""

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nnnu.services.embedding.base import EmbeddingError
from nnnu.services.knowledge import manifest as store
from nnnu.services.knowledge.service import MAX_KB_UPLOAD_BYTES, KBError
from nnnu.services.knowledge.types import DOC_DELETED, KbDoc

router = APIRouter()

SEARCH_MODES = ("vector", "hybrid", "auto")
MAX_TOP_K = 50


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _live_doc(service, kb_id: str, doc_id: str) -> KbDoc | None:
    """取一份还在册的文档：不存在或已删除都算没了（删掉的东西不该还能下载）。"""
    manifest = service.get_kb(kb_id)
    doc = manifest.doc(doc_id) if manifest else None
    if doc is None or doc.status == DOC_DELETED:
        return None
    return doc


class CreateKbRequest(BaseModel):
    name: str


class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"
    top_k: int = 5


class ReindexRequest(BaseModel):
    force: bool = True


@router.get("/api/v1/kbs")
async def list_kbs(http_request: Request):
    manifests = http_request.app.state.kb.list_kbs()
    return {"kbs": [manifest.model_dump() for manifest in manifests]}


@router.post("/api/v1/kbs")
async def create_kb(body: CreateKbRequest, http_request: Request):
    try:
        manifest = await http_request.app.state.kb.create_kb(body.name)
    except store.KbNameError as exc:
        return _error(422, "invalid_name", str(exc))
    return manifest.model_dump()


@router.get("/api/v1/kbs/{kb_id}")
async def get_kb(kb_id: str, http_request: Request):
    manifest = http_request.app.state.kb.get_kb(kb_id)
    if manifest is None:
        return _not_found(f"知识库 {kb_id} 不存在")
    return manifest.model_dump()


@router.delete("/api/v1/kbs/{kb_id}")
async def delete_kb(kb_id: str, http_request: Request):
    if not await http_request.app.state.kb.delete_kb(kb_id):
        return _not_found(f"知识库 {kb_id} 不存在")
    return {"deleted": kb_id}


@router.post("/api/v1/kbs/{kb_id}/docs")
async def upload_doc(kb_id: str, http_request: Request, file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_KB_UPLOAD_BYTES:
        return _error(
            422,
            "doc_too_large",
            f"文档超过大小上限（{MAX_KB_UPLOAD_BYTES // (1024 * 1024)}MB）",
        )
    service = http_request.app.state.kb
    if service.get_kb(kb_id) is None:
        return _not_found(f"知识库 {kb_id} 不存在")
    try:
        doc = await service.add_doc(kb_id, file.filename or "unnamed", content, file.content_type)
    except KBError as exc:  # 类型不支持 / 超额 / 库刚被删
        return _error(422, "invalid_request", str(exc))
    return doc.model_dump()


@router.get("/api/v1/kbs/{kb_id}/docs/{doc_id}")
async def get_doc(kb_id: str, doc_id: str, http_request: Request):
    manifest = http_request.app.state.kb.get_kb(kb_id)
    doc = manifest.doc(doc_id) if manifest else None
    if doc is None:
        return _not_found(f"文档 {doc_id} 不存在")
    return doc.model_dump()


@router.delete("/api/v1/kbs/{kb_id}/docs/{doc_id}")
async def delete_doc(kb_id: str, doc_id: str, http_request: Request):
    if not await http_request.app.state.kb.delete_doc(kb_id, doc_id):
        return _not_found(f"文档 {doc_id} 不存在")
    return {"deleted": doc_id}


@router.get("/api/v1/kbs/{kb_id}/docs/{doc_id}/file")
async def download_doc(kb_id: str, doc_id: str, http_request: Request):
    """原件下载/内嵌（阅读器用）：PDF 交给浏览器原生渲染，文本类走 /content。"""
    service = http_request.app.state.kb
    doc = _live_doc(service, kb_id, doc_id)
    if doc is None:
        return _not_found(f"文档 {doc_id} 不存在")
    path = service.doc_file(kb_id, doc_id)
    if path is None:
        return _not_found("文档不存在或文件已被清理")
    return FileResponse(
        path, filename=doc.filename, media_type=doc.mime or "application/octet-stream"
    )


@router.get("/api/v1/kbs/{kb_id}/docs/{doc_id}/content")
async def doc_content(kb_id: str, doc_id: str, http_request: Request):
    """解析结果（文本类阅读器）：给整页 text，前端 <pre> 直接铺。"""
    service = http_request.app.state.kb
    doc = _live_doc(service, kb_id, doc_id)
    if doc is None:
        return _not_found(f"文档 {doc_id} 不存在")
    parsed = service.doc_parsed(kb_id, doc_id)
    if parsed is None:
        return _error(409, "not_parsed", "这份文档还没有解析结果（正在处理或解析失败）")
    return {
        "doc_id": doc.doc_id,
        "filename": doc.filename,
        "kind": parsed.get("kind", "text"),
        "page_count": parsed.get("page_count", 0),
        "text": parsed.get("text", ""),
    }


@router.post("/api/v1/kbs/{kb_id}/search")
async def search_kb(kb_id: str, body: SearchRequest, http_request: Request):
    """检索测试台：单库检索，命中带页码与片段（§7.9 验收 A/B 的人工观察口）。"""
    if body.mode not in SEARCH_MODES:
        return _error(422, "invalid_mode", f"mode 只能是 {SEARCH_MODES} 之一")
    if not body.query.strip():
        return _error(422, "invalid_request", "query 不能为空")
    service = http_request.app.state.kb
    if service.get_kb(kb_id) is None:
        return _not_found(f"知识库 {kb_id} 不存在")
    try:
        hits = await service.search(
            kb_id,
            body.query,
            mode=body.mode,
            top_k=max(1, min(int(body.top_k), MAX_TOP_K)),
        )
    except EmbeddingError as exc:  # 嵌入端不可用：说清楚，别让前端只看 500
        return _error(503, "embedding_unavailable", str(exc), recoverable=True)
    return {"mode": body.mode, "query": body.query, "hits": [hit.model_dump() for hit in hits]}


@router.post("/api/v1/kbs/{kb_id}/reindex")
async def reindex_kb(kb_id: str, http_request: Request, body: ReindexRequest | None = None):
    force = True if body is None else body.force
    try:
        manifest = await http_request.app.state.kb.reindex(kb_id, force=force)
    except KBError as exc:
        return _not_found(str(exc))
    return manifest.model_dump()


@router.post("/api/v1/kbs/{kb_id}/build/cancel")
async def cancel_build(kb_id: str, http_request: Request):
    if not await http_request.app.state.kb.cancel_build(kb_id):
        return _error(409, "no_build", "这个库现在没有在跑的构建")
    return {"cancelled": kb_id}


@router.get("/api/v1/kbs/{kb_id}/sources")
async def get_sources(kb_id: str, http_request: Request):
    return _error(
        501, "not_implemented", "外部源绑定（GitHub / 外部库）在 P14 交付", recoverable=False
    )


@router.put("/api/v1/kbs/{kb_id}/sources")
async def put_sources(kb_id: str, http_request: Request):
    return _error(
        501, "not_implemented", "外部源绑定（GitHub / 外部库）在 P14 交付", recoverable=False
    )
