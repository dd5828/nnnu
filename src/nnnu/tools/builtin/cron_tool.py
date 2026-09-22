"""cron 工具（§7.2）：创建/删除/列出定时任务，到点以新回合执行。

context_gated：用户已有启用中的定时任务时挂载（§6.3"有定时任务 → cron"）。
任务内容在到点时作为用户消息发起新回合（执行器由 lifespan 接 TurnRuntime）。
"""

from datetime import datetime

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.cron.scheduler import CronExprError, get_cron_service


class CronTool(BaseTool):
    definition = ToolDefinition(
        name="cron",
        description="tools.cron",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "delete"],
                    "description": "操作类型",
                },
                "schedule": {
                    "type": "string",
                    "description": "cron 表达式（分 时 日 月 周），如 '0 8 * * *' 每天 8 点（create 必填）",
                },
                "prompt": {
                    "type": "string",
                    "description": "到点执行的任务内容（create 必填，如「给我出一道线性代数题」）",
                },
                "job_id": {"type": "string", "description": "要删除的任务 ID（delete 必填）"},
            },
            "required": ["action"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="定时任务（到点自动发起新回合，消耗 token）",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        action = str(ctx.args.get("action", ""))
        service = get_cron_service()
        try:
            if action == "create":
                schedule = str(ctx.args.get("schedule", "")).strip()
                prompt = str(ctx.args.get("prompt", "")).strip()
                job = await service.create(schedule, prompt, session_id=ctx.session_id)
                next_run = datetime.fromtimestamp(job.next_run_at).strftime("%Y-%m-%d %H:%M")
                return ToolResult(
                    ok=True,
                    output=f"已创建定时任务 {job.id}：{job.schedule}（下次执行 {next_run}）",
                    detail={
                        "job_id": job.id,
                        "schedule": job.schedule,
                        "next_run_at": job.next_run_at,
                    },
                )
            if action == "list":
                jobs = await service.list_jobs()
                if not jobs:
                    return ToolResult(ok=True, output="当前没有定时任务。")
                lines = [f"共 {len(jobs)} 个定时任务："]
                for job in jobs:
                    next_run = datetime.fromtimestamp(job.next_run_at).strftime("%Y-%m-%d %H:%M")
                    state = "启用" if job.enabled else "停用"
                    lines.append(
                        f"- {job.id} [{state}] {job.schedule} → {job.prompt[:60]}（下次 {next_run}）"
                    )
                return ToolResult(ok=True, output="\n".join(lines))
            if action == "delete":
                job_id = str(ctx.args.get("job_id", "")).strip()
                if not job_id:
                    return ToolResult(ok=False, output="delete 操作需要 job_id。")
                deleted = await service.delete(job_id)
                if not deleted:
                    return ToolResult(ok=False, output=f"任务不存在：{job_id}")
                return ToolResult(ok=True, output=f"已删除定时任务 {job_id}。")
            return ToolResult(ok=False, output=f"未知操作 {action}（create/list/delete）")
        except CronExprError as exc:
            return ToolResult(ok=False, output=str(exc))
