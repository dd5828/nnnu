"""成本汇总 REST（§6.9）：GET /api/v1/cost/summary?days=7。"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/v1/cost/summary")
async def cost_summary(http_request: Request, days: int = 7):
    return await http_request.app.state.runtime._costs.query_summary(days=days)
