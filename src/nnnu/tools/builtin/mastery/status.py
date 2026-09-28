"""`mastery_status`：无参只读，一次拿到路径地图 + 下一目标 + 未决题 + 薄弱点 + 复习安排。

**每轮第一条工具必须调它**（提示词硬性规则）：服务端不再往 system 提示词里塞状态块，
模型要什么自己来取——这样「模型看到的状态」和「库里此刻的状态」永远是同一份。
"""

from __future__ import annotations

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.service import get_learning_service
from nnnu.tools.builtin.mastery.common import no_path, preview, resolve_path, status_lines


class MasteryStatusTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_status",
        description="tools.mastery_status",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.mastery_status",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        path = await resolve_path(ctx, service)
        if path is None:
            # 「还没建树」是正常状态，不是调用出错：照样 ok=True，让模型接着 build 或 switch
            return (await no_path(ctx, service)).model_copy(update={"ok": True})
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        pending = await service.pending_interaction(path.id)
        lines = [status_lines(ctx, detail)]
        if pending is not None:
            lines.append(
                get_prompt_manager()
                .render(
                    "mastery",
                    ctx.language,
                    "state_block.pending",
                    stem=preview(pending.card_prompt),
                )
                .strip()
            )
        payload = detail.model_dump()
        payload["action"] = "status"
        payload["pending"] = (
            {
                "interaction_id": pending.id,
                "question_id": pending.question_id,
                "node_id": pending.node_id,
                "kind": pending.kind,
                "status": pending.status,
                "stem": pending.card_prompt,
                "answered": bool(pending.user_answer),
            }
            if pending is not None
            else None
        )
        return ToolResult(ok=True, output="\n".join(line for line in lines if line), detail=payload)
