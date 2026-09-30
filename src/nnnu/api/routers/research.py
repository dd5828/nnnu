"""深度研究 REST（§9.1 之外补的两个只读端点，见 STAGE_LOG 偏离清单）。

调研本身全程走 WS（不新增回合类端点），这里只把 `research_runs` 这张草稿本
读出来给「我的历次调研」页用：

- `GET /api/v1/research/runs`：历次调研列表（新的在前，带会话标题与总数）。
- `GET /api/v1/research/runs/{run_id}`：一次调研的详情——大纲 + 报告正文 + 来源。

报告正文不存库（助理消息 id 在能力返回之后才生成，见 schema v10 注释），详情端点在
这里现查：确认消息之后的第一条 assistant 消息（`ResearchService.report_of`）。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nnnu.services.research.models import RunReport, RunSummary
from nnnu.services.research.service import ResearchService

router = APIRouter()

# 一页最多 200 条：列表不做无限滚动，够用就行（单机单人的调研次数以十计）
MAX_LIMIT = 200


def _service(http_request: Request) -> ResearchService:
    return http_request.app.state.research


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> JSONResponse:
    """§9.1 统一错误信封 {error:{code,message,recoverable}}。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "recoverable": recoverable}},
    )


def _summary_dict(summary: RunSummary) -> dict:
    return {**summary.run.model_dump(), "session_title": summary.session_title}


def _report_dict(report: RunReport | None) -> dict | None:
    # 正文与来源分开给：前端正文走 Markdown、来源走 CitesPanel（跟聊天页同一套组件）
    return report.model_dump() if report is not None else None


@router.get("/api/v1/research/runs")
async def list_research_runs(http_request: Request, limit: int = 50, offset: int = 0):
    service = _service(http_request)
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    summaries = await service.list_runs(limit=limit, offset=offset)
    return {
        "runs": [_summary_dict(item) for item in summaries],
        "total": await service.count_runs(),
    }


@router.get("/api/v1/research/runs/{run_id}")
async def get_research_run(run_id: str, http_request: Request):
    service = _service(http_request)
    run = await service.get_run(run_id)
    if run is None:
        return _error(404, "not_found", f"调研 {run_id} 不存在")
    # 会话标题单独取一次：详情页顶上要显示「在哪条会话里跑的」
    session = await http_request.app.state.runtime._sessions.get_session(run.session_id)
    return {
        **_summary_dict(RunSummary(run=run, session_title=session.title if session else "")),
        "report": _report_dict(await service.report_of(run)),
    }
