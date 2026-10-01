"""记忆 REST（§7.10）：三层浏览 + 证据链图谱 + 整合触发 + L2/L3 条目编辑。

- 路径都在装饰器里写全（本仓库路由不带 prefix，照 `knowledge.py`）。
  **声明顺序**：固定路径（`/memory/graph`、`/memory/consolidate`）与具名层路径
  （`/memory/l1`…）必须排在参数化路径 `/memory/{layer}/{entry_id}` 之前。
- 错误码（§9.1 统一信封）：`not_found` 404 / `read_only_layer` 409 / `busy` 409 /
  `invalid_entry` 422 / `memory_disabled` 503。
- L1 只读（`read_only_layer`）：轨迹 append-only，行号即引用锚点，删行会让后续
  引用 seq 校验全断；条目的编辑/删除只落在 L2/L3。
- 手写未纳管的匿名条目（无 `[mem-…]`）不能被 PATCH 寻址——下轮 consolidator
  的 audit 会给它补 id 并标为人工所有，之后就能在工作台编辑。
"""

import re
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.memory import graph, paths, trace
from nnnu.services.memory.consolidator.pipeline import MODES
from nnnu.services.memory.models import text_digest
from nnnu.services.memory.service import MemoryService
from nnnu.services.memory.state import doc_key
from nnnu.services.memory.store import render_doc

router = APIRouter()

ENTRY_ID_RE = re.compile(r"^mem-[0-9a-f]{8}$")
MAX_L1_PAGE = 500
# 条目正文上限（与 store 解析口径一致：L2 400 字 / L3 300 字）
TEXT_LIMITS = {"l2": 400, "l3": 300}


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", what)


def _disabled() -> JSONResponse:
    return _error(503, "memory_disabled", "记忆服务未装配")


def _service(http_request: Request) -> MemoryService | None:
    return getattr(http_request.app.state, "memory", None)


def _doc_view(service: MemoryService, layer: str, key: str) -> dict:
    """一份 L2/L3 文档的展示视图（文本 + 条目 + 计数）。"""
    doc = service.store.load(layer, key)
    entries = [graph.entry_view(service.state, entry) for entry in doc.entries]
    return {
        "layer": layer,
        "key": key,
        "text": render_doc(doc),
        "entries": entries,
        "stats": {
            "entries": len(entries),
            "stale": sum(1 for entry in entries if entry["stale"]),
            "edited": sum(1 for entry in entries if entry["edited"]),
            "anonymous": sum(1 for entry in entries if entry["id"] is None),
        },
    }


class ConsolidateRequest(BaseModel):
    modes: list[str] | None = None
    surfaces: list[str] | None = None


class EntryPatchRequest(BaseModel):
    text: str | None = None
    managed: bool = False


@router.get("/api/v1/memory")
async def memory_overview(http_request: Request):
    """三层概览：计数、水位、待整合回合数、上次运行、是否在跑。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    state = service.state
    l1: dict[str, dict] = {}
    for surface in paths.SURFACES:
        files = trace.list_files(service.data_root, surface)
        lines = 0
        size = 0
        events: dict[str, int] = {}
        for name in files:
            # files 里是含后缀的文件名（trace_path 收的是 yyyy-mm，会拼成 …jsonl.jsonl）
            stats = trace.file_stats(paths.trace_dir(service.data_root, surface) / name)
            lines += stats["lines"]
            size += stats["bytes"]
            for kind, count in stats["events"].items():
                events[kind] = events.get(kind, 0) + count
        l1[surface] = {"files": files, "lines": lines, "bytes": size, "events": events}
    return {
        "trace_enabled": service.config().trace_enabled,
        "surfaces": list(paths.SURFACES),
        "l3_docs": list(paths.L3_DOCS),
        "l1": l1,
        "l2": {surface: _doc_view(service, "l2", surface)["stats"] for surface in paths.SURFACES},
        "l3": {doc: _doc_view(service, "l3", doc)["stats"] for doc in paths.L3_DOCS},
        "watermarks": {surface: state.watermark(surface) for surface in paths.SURFACES},
        "turns_since_consolidation": state.turns_since(),
        "config": asdict(service.config()),
        "last_run": state.last_run(),
        "consolidating": service.busy,
    }


@router.get("/api/v1/memory/l1")
async def memory_l1(
    http_request: Request,
    surface: str,
    file: str | None = None,
    offset: int = 0,
    limit: int = 200,
):
    """L1 行分页（只读）；file 省略取最新月文件。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if surface not in paths.SURFACES:
        return _not_found(f"面 {surface}")
    files = trace.list_files(service.data_root, surface)
    if file is not None and file not in files:
        return _not_found(f"文件 {file}")
    name = file or (files[-1] if files else "")
    offset = max(0, offset)
    limit = max(1, min(MAX_L1_PAGE, limit))
    total, rows = (
        trace.read_page(service.data_root, surface, name, offset=offset, limit=limit)
        if name
        else (0, [])
    )
    return {
        "surface": surface,
        "file": name,
        "files": files,
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": rows,
    }


@router.get("/api/v1/memory/l2")
async def memory_l2(http_request: Request, surface: str | None = None):
    """L2 文档（surface 省略返回全部七面）。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if surface is not None and surface not in paths.SURFACES:
        return _not_found(f"面 {surface}")
    keys = [surface] if surface else list(paths.SURFACES)
    return {"layer": "l2", "docs": [_doc_view(service, "l2", key) for key in keys]}


@router.get("/api/v1/memory/l3")
async def memory_l3(http_request: Request, doc: str | None = None):
    """L3 文档（doc 省略返回全部四篇）。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if doc is not None and doc not in paths.L3_DOCS:
        return _not_found(f"文档 {doc}")
    keys = [doc] if doc else list(paths.L3_DOCS)
    return {"layer": "l3", "docs": [_doc_view(service, "l3", key) for key in keys]}


@router.get("/api/v1/memory/graph")
async def memory_graph(http_request: Request, entry: str | None = None, depth: int = 1):
    """证据链：entry 省略给全景，给了就以其为根按 depth（1|2）展开。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if depth not in (1, 2):
        return _error(422, "invalid_entry", "depth 只支持 1 或 2")
    if entry is not None and not ENTRY_ID_RE.fullmatch(entry):
        return _error(422, "invalid_entry", "条目 id 形如 mem-xxxxxxxx")
    payload = graph.build_graph(
        service.data_root, service.store, service.state, entry=entry, depth=depth
    )
    if payload is None:
        return _not_found(f"条目 {entry}")
    return payload


@router.post("/api/v1/memory/consolidate")
async def memory_consolidate(body: ConsolidateRequest, http_request: Request):
    """手动触发一轮整合：后台跑，202 回 run（即刻可轮询）；已在跑 409。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if body.modes is not None:
        bad = [mode for mode in body.modes if mode not in MODES]
        if bad:
            return _error(422, "invalid_entry", f"未知模式 {bad}（可用：{list(MODES)}）")
    if body.surfaces is not None:
        bad = [surface for surface in body.surfaces if surface not in paths.SURFACES]
        if bad:
            return _error(422, "invalid_entry", f"未知面 {bad}")
    run = service.start_consolidation(trigger="manual", modes=body.modes, surfaces=body.surfaces)
    if run is None:
        return _error(409, "busy", "已有一轮整合在跑")
    return JSONResponse(status_code=202, content={"started": True, "run": run.to_dict()})


@router.patch("/api/v1/memory/{layer}/{entry_id}")
async def memory_patch_entry(
    layer: str, entry_id: str, body: EntryPatchRequest, http_request: Request
):
    """改条目正文（人工编辑 → 进保护集）或交还自动管理（managed=true）。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if layer == "l1":
        return _error(409, "read_only_layer", "L1 轨迹只读（append-only，行号引用完整性）")
    if layer not in TEXT_LIMITS:
        return _not_found(f"层 {layer}")
    found = service.store.find(layer, entry_id)
    if found is None:
        return _not_found(f"条目 {entry_id}")
    key, entry = found
    state_key = doc_key(layer, key)
    if body.managed:  # 交还自动管理：之后 audit/dedup 可以再改它
        service.state.set_managed(state_key, entry_id, text_digest(entry.text))
        return {"layer": layer, "key": key, "entry": graph.entry_view(service.state, entry)}
    if body.text is None:
        return _error(422, "invalid_entry", "text 与 managed 至少要给一个")
    text = body.text.strip()
    if not text:
        return _error(422, "invalid_entry", "正文不能为空")
    limit = TEXT_LIMITS[layer]
    if len(text) > limit:
        return _error(422, "invalid_entry", f"正文上限 {limit} 字")

    def mutate(doc) -> None:
        for item in doc.entries:
            if item.id == entry_id:
                item.text = text

    await service.store.apply(layer, key, mutate)
    service.state.mark_edited(state_key, entry_id, text_digest(text))
    updated = service.store.find_in(layer, key, entry_id)
    return {
        "layer": layer,
        "key": key,
        "entry": graph.entry_view(service.state, updated or entry),
    }


@router.delete("/api/v1/memory/{layer}/{entry_id}")
async def memory_delete_entry(layer: str, entry_id: str, http_request: Request):
    """删条目 + 记 tombstone（防 LLM 下轮又写回来；L3 引用下轮 audit 自然标 STALE）。"""
    service = _service(http_request)
    if service is None:
        return _disabled()
    if layer == "l1":
        return _error(409, "read_only_layer", "L1 轨迹只读（append-only，行号引用完整性）")
    if layer not in TEXT_LIMITS:
        return _not_found(f"层 {layer}")
    found = service.store.find(layer, entry_id)
    if found is None:
        return _not_found(f"条目 {entry_id}")
    key, _ = found

    def mutate(doc) -> None:
        doc.entries = [item for item in doc.entries if item.id != entry_id]

    await service.store.apply(layer, key, mutate)
    service.state.drop_entry_meta(doc_key(layer, key), entry_id)
    service.state.add_tombstone(entry_id, layer, key)
    return {"deleted": entry_id, "layer": layer, "key": key}
