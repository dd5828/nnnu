"""知识库 REST（§9.1）：列表/创建/删除、文档上传与进度、检索测试台、重建与取消。

补充端点（开发方案 §9.1 之外，记在 STAGE_LOG 偏离清单里）：
- `POST /kbs/{id}/build/cancel`：§7.9 要求构建可中断，没有对应端点就落不了地；
- `GET /kbs/{id}/docs/{doc_id}/file`：阅读器直接看原件（PDF 用 iframe + #page=N）；
- `GET /kbs/{id}/docs/{doc_id}/content`：文本类文档的解析结果，前端 <pre> 渲染；
- `POST /kbs/search`：跨全部就绪库检索（知识中心列表页入口，用 service.search_all_ready）。

`/sources`（GitHub 源）P14 起由 `github_sources` 路由接管（见 api/routers/github_sources.py）。
检索模式：单库按当前活跃版本的引擎收紧（vector 不认 local/global 之类图谱模式），
跨库检索只做全集校验，各引擎自己归一。
"""

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from nnnu.services.embedding.base import EmbeddingError
from nnnu.services.knowledge import manifest as store
from nnnu.services.knowledge.service import MAX_KB_UPLOAD_BYTES, KBError
from nnnu.services.knowledge.types import DOC_DELETED, KbDoc
from nnnu.services.rag.base import EngineUnavailable
from nnnu.services.rag.pipelines.graph_common import EngineProcessError
from nnnu.services.rag.registry import (
    DEFAULT_ENGINE,
    engine_info,
    engine_infos,
)

router = APIRouter()

# 已知检索模式的全集；单库检索再按引擎的 supported_modes 收紧
SEARCH_MODES = (
    "vector",
    "hybrid",
    "auto",
    "local",
    "global",
    "drift",
    "basic",
    "naive",
    "mix",
)
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
    engine: str = DEFAULT_ENGINE


class SetEngineRequest(BaseModel):
    engine: str


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


@router.get("/api/v1/kbs/engines")
async def list_engines():
    """可选检索引擎（含没装依赖的：带 available=false 与安装提示）。"""
    return {"engines": [info.payload() for info in engine_infos()]}


@router.post("/api/v1/kbs")
async def create_kb(body: CreateKbRequest, http_request: Request):
    try:
        manifest = await http_request.app.state.kb.create_kb(body.name, engine=body.engine)
    except store.KbNameError as exc:
        return _error(422, "invalid_name", str(exc))
    except KBError as exc:  # 引擎不可用
        return _error(422, "engine_unavailable", str(exc))
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


@router.put("/api/v1/kbs/{kb_id}/engine")
async def set_kb_engine(kb_id: str, body: SetEngineRequest, http_request: Request):
    """换引擎：校验依赖 → 改 manifest.engine → 后台强制重建（旧版本在重建期间照常可查）。"""
    service = http_request.app.state.kb
    if service.get_kb(kb_id) is None:
        return _not_found(f"知识库 {kb_id} 不存在")
    try:
        info = engine_info(body.engine)
    except EngineUnavailable as exc:
        return _error(422, "engine_unavailable", str(exc))
    if not info.available:
        return _error(422, "engine_unavailable", info.reason)
    try:
        manifest = await service.set_engine(kb_id, body.engine)
    except KBError as exc:
        return _error(409, "engine_switch_failed", str(exc), recoverable=True)
    return manifest.model_dump()


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
        path,
        filename=doc.filename,
        media_type=doc.mime or "application/octet-stream",
        # inline：PDF iframe 内嵌与图片直显需要浏览器渲染而不是下载
        content_disposition_type="inline",
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


# 静态路径声明在 `{kb_id}` 之前（与 settings/probe、settings/llm-options 同款注意事项：
# 往后加 POST /kbs/xxx 形状的端点时别再往 `{kb_id}` 路由后面塞）
@router.post("/api/v1/kbs/search")
async def search_all_kbs(body: SearchRequest, http_request: Request):
    """全库检索：跨全部就绪的库，命中带各自库名（知识中心列表页的入口）。

    跨库排序口径见 `KBService.search_all_ready`：同查询内相对余弦加权 +
    字面命中加成，只吃名次不吃分数（分数尺度跨库不可比）。
    """
    if body.mode not in SEARCH_MODES:
        return _error(422, "invalid_mode", f"mode 只能是 {SEARCH_MODES} 之一")
    if not body.query.strip():
        return _error(422, "invalid_request", "query 不能为空")
    try:
        hits = await http_request.app.state.kb.search_all_ready(
            body.query,
            mode=body.mode,
            top_k=max(1, min(int(body.top_k), MAX_TOP_K)),
        )
    except EmbeddingError as exc:  # 嵌入端不可用：说清楚，别让前端只看 500
        return _error(503, "embedding_unavailable", str(exc), recoverable=True)
    return {"mode": body.mode, "query": body.query, "hits": [hit.model_dump() for hit in hits]}


@router.post("/api/v1/kbs/{kb_id}/search")
async def search_kb(kb_id: str, body: SearchRequest, http_request: Request):
    """检索测试台：单库检索，命中带页码与片段（§7.9 验收 A/B 的人工观察口）。"""
    if body.mode not in SEARCH_MODES:
        return _error(422, "invalid_mode", f"mode 只能是 {SEARCH_MODES} 之一")
    if not body.query.strip():
        return _error(422, "invalid_request", "query 不能为空")
    service = http_request.app.state.kb
    manifest = service.get_kb(kb_id)
    if manifest is None:
        return _not_found(f"知识库 {kb_id} 不存在")
    engine_id = service.version_engine(manifest, manifest.active_version)
    try:
        allowed = engine_info(engine_id).modes
    except EngineUnavailable:
        allowed = SEARCH_MODES
    if body.mode not in allowed:
        return _error(
            422,
            "invalid_mode",
            f"这个库当前的 {engine_id} 引擎支持 {'/'.join(allowed)}，不支持 {body.mode}",
        )
    try:
        hits = await service.search(
            kb_id,
            body.query,
            mode=body.mode,
            top_k=max(1, min(int(body.top_k), MAX_TOP_K)),
        )
    except EmbeddingError as exc:  # 嵌入端不可用：说清楚，别让前端只看 500
        return _error(503, "embedding_unavailable", str(exc), recoverable=True)
    except (EngineUnavailable, EngineProcessError) as exc:
        return _error(503, "engine_error", str(exc), recoverable=True)
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


# GitHub 源绑定见 api/routers/github_sources.py（P14）；这里不再留 501 空壳。
