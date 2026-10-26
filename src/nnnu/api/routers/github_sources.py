"""GitHub 源 REST（§7.9，P14）：绑定、检查漂移、手动同步。

端点（挂在知识库下面）：
- `GET    /kbs/{id}/sources`                  当前绑定的源
- `POST   /kbs/{id}/sources`                  新增（可选立刻同步一次）
- `PUT    /kbs/{id}/sources/{sid}`            改仓库/分支/glob/自动同步
- `DELETE /kbs/{id}/sources/{sid}`            解绑（默认连同步来的文档一起删）
- `POST   /kbs/{id}/sources/{sid}/check`      只比对远端，不改文档
- `POST   /kbs/{id}/sources/{sid}/sync`       按差异下载并增改删文档
- `POST   /kbs/{id}/sources/check|sync`       这个库的全部源各来一遍

错误信封与知识库路由一致（§9.1）。GitHub 侧失败返回 502，源配置不合法 422。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from nnnu.services.github_source.client import GithubError
from nnnu.services.github_source.service import (
    GithubSourceService,
    SourceError,
    SourceNotFound,
)
from nnnu.services.github_source.types import DEFAULT_INTERVAL_MINUTES
from nnnu.services.knowledge.service import KBError

router = APIRouter()


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _service(http_request: Request) -> GithubSourceService:
    return http_request.app.state.github_sources


def _source_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SourceNotFound):
        return _error(404, "not_found", str(exc))
    if isinstance(exc, GithubError):
        return _error(502, "github_error", str(exc), recoverable=True)
    if isinstance(exc, KBError):
        return _error(409, "kb_busy", str(exc), recoverable=True)
    return _error(422, "invalid_source", str(exc))


class AddSourceRequest(BaseModel):
    repo: str
    branch: str = "main"
    glob: str = "**/*.md"
    auto_sync: bool = True
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    sync_now: bool = True


class UpdateSourceRequest(BaseModel):
    repo: str | None = None
    branch: str | None = None
    glob: str | None = None
    auto_sync: bool | None = None
    interval_minutes: int | None = None
    sync_now: bool = False


@router.get("/api/v1/kbs/{kb_id}/sources")
async def list_sources(kb_id: str, http_request: Request):
    try:
        sources = _service(http_request).list_sources(kb_id)
    except SourceError as exc:
        return _source_error(exc)
    return {"sources": [source.model_dump() for source in sources]}


@router.post("/api/v1/kbs/{kb_id}/sources")
async def add_source(kb_id: str, body: AddSourceRequest, http_request: Request):
    service = _service(http_request)
    try:
        source = await service.add_source(
            kb_id,
            repo=body.repo,
            branch=body.branch,
            glob=body.glob,
            auto_sync=body.auto_sync,
            interval_minutes=body.interval_minutes,
        )
    except (SourceError, GithubError) as exc:
        return _source_error(exc)
    sync_error = ""
    report = None
    if body.sync_now:
        try:
            report = await service.sync_source(kb_id, source.source_id)
        except (SourceError, GithubError, KBError) as exc:
            # 源已经保存了，同步失败不该让前端以为「没绑定成功」；错误单独带回去，
            # 用户可改配置或补 token 后点「同步」
            sync_error = str(exc)
        source = service.get_source(kb_id, source.source_id)
    return {
        "source": source.model_dump(),
        "sync": report.model_dump() if report else None,
        "sync_error": sync_error,
    }


@router.put("/api/v1/kbs/{kb_id}/sources/{source_id}")
async def update_source(
    kb_id: str, source_id: str, body: UpdateSourceRequest, http_request: Request
):
    service = _service(http_request)
    try:
        source = await service.update_source(
            kb_id,
            source_id,
            repo=body.repo,
            branch=body.branch,
            glob=body.glob,
            auto_sync=body.auto_sync,
            interval_minutes=body.interval_minutes,
        )
    except (SourceError, GithubError) as exc:
        return _source_error(exc)
    report = None
    if body.sync_now:
        try:
            report = await service.sync_source(kb_id, source_id)
        except (SourceError, GithubError, KBError) as exc:
            return _error(502, "sync_failed", str(exc), recoverable=True)
    return {"source": source.model_dump(), "sync": report.model_dump() if report else None}


@router.delete("/api/v1/kbs/{kb_id}/sources/{source_id}")
async def delete_source(
    kb_id: str, source_id: str, http_request: Request, remove_docs: bool = True
):
    try:
        source = await _service(http_request).delete_source(
            kb_id, source_id, remove_docs=remove_docs
        )
    except (SourceError, KBError) as exc:
        return _source_error(exc)
    return {"deleted": source.source_id, "docs_removed": remove_docs}


@router.post("/api/v1/kbs/{kb_id}/sources/{source_id}/check")
async def check_source(kb_id: str, source_id: str, http_request: Request):
    try:
        report = await _service(http_request).check_source(kb_id, source_id)
    except (SourceError, GithubError) as exc:
        return _source_error(exc)
    return report.model_dump()


@router.post("/api/v1/kbs/{kb_id}/sources/{source_id}/sync")
async def sync_source(kb_id: str, source_id: str, http_request: Request):
    try:
        report = await _service(http_request).sync_source(kb_id, source_id)
    except (SourceError, GithubError, KBError) as exc:
        return _source_error(exc)
    return report.model_dump()


@router.post("/api/v1/kbs/{kb_id}/sources/check")
async def check_all(kb_id: str, http_request: Request):
    try:
        reports = await _service(http_request).check_all(kb_id)
    except SourceError as exc:
        return _source_error(exc)
    return {"reports": [report.model_dump() for report in reports]}


@router.post("/api/v1/kbs/{kb_id}/sources/sync")
async def sync_all(kb_id: str, http_request: Request):
    try:
        reports = await _service(http_request).sync_all(kb_id)
    except SourceError as exc:
        return _source_error(exc)
    return {"reports": [report.model_dump() for report in reports]}
