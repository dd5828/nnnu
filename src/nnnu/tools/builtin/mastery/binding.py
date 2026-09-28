"""绑定三件：列路径、切路径、脱离路径（三个都是零 LLM 的纯读写）。

切路径是**先释放再绑定**的原子交接：`bind_session` 内部会把本会话原来栓的那条解绑
（`_unbind_others`），所以「切到 B」不会留下「会话同时绑着 A 和 B」这种自相矛盾的状态。
"""

from __future__ import annotations

import sqlite3

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.service import LearningService, get_learning_service
from nnnu.tools.builtin.mastery.common import action_label, resolve_path, status_lines

_COST_HINT = "tools.cost.mastery_paths"


def _service() -> LearningService | ToolResult:
    """取学习服务；没装配就返回一个友好报错（不抛穿整个回合）。"""
    try:
        return get_learning_service()
    except RuntimeError as exc:
        return ToolResult(ok=False, output=str(exc))


class MasteryPathsTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_paths",
        description="tools.mastery_paths",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint=_COST_HINT,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        service = _service()
        if isinstance(service, ToolResult):
            return service
        summaries = await service.list_paths()
        if not summaries:
            # 「一条路径都还没有」是正常状态，不是调用出错：给指路，别标成 ok=False
            return ToolResult(
                ok=True,
                output="还没有任何学习路径：用 mastery_build 建一条（给 topic 与 nodes）。",
                detail={"paths": []},
            )
        prompts = get_prompt_manager()
        lines = [prompts.render("mastery", ctx.language, "state_block.paths_intro").strip()]
        rows: list[dict] = []
        for summary in summaries:
            lines.append(
                prompts.render(
                    "mastery",
                    ctx.language,
                    "state_block.path_line",
                    title=summary.path.title,
                    mastered=summary.stats.mastered,
                    total=summary.stats.total,
                    due=summary.stats.due,
                    next=summary.next_title or "—",
                    action=action_label(prompts, ctx.language, summary.next_action),
                    id=summary.path.id,
                ).strip()
            )
            rows.append(
                {
                    "path_id": summary.path.id,
                    "title": summary.path.title,
                    "stats": summary.stats.model_dump(),
                    "next_title": summary.next_title,
                    "next_action": summary.next_action,
                    "bound": summary.path.session_id == ctx.session_id,
                }
            )
        lines.append(prompts.render("mastery", ctx.language, "state_block.paths_hint").strip())
        return ToolResult(
            ok=True, output="\n".join(line for line in lines if line), detail={"paths": rows}
        )


class MasterySwitchTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_switch",
        description="tools.mastery_switch",
        parameters={
            "type": "object",
            "properties": {
                "path_id": {
                    "type": "string",
                    "description": "要切过去的路径 id（先用 mastery_paths 看有哪些）",
                }
            },
            "required": ["path_id"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint=_COST_HINT,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        service = _service()
        if isinstance(service, ToolResult):
            return service
        target_id = str(ctx.args.get("path_id") or "").strip()
        if not target_id:
            return ToolResult(ok=False, output="需要 path_id（先用 mastery_paths 看有哪些）。")
        path = await service.get_path_model(target_id)
        if path is None:
            return ToolResult(ok=False, output=f"学习路径 {target_id} 不存在。")
        if not ctx.session_id:
            return ToolResult(ok=False, output="当前没有会话上下文，切换不了路径。")
        try:
            await service.bind_session(path.id, ctx.session_id)
        except sqlite3.IntegrityError:
            return ToolResult(ok=False, output=f"切换路径时写冲突（{path.id}），重做一次就好。")
        detail = await service.get_path(path.id)
        return ToolResult(
            ok=True,
            output=f"已切到路径《{path.title}》：\n{status_lines(ctx, detail)}",
            detail=detail.model_dump() if detail else {"path_id": path.id},
        )


class MasteryLeaveTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_leave",
        description="tools.mastery_leave",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint=_COST_HINT,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        service = _service()
        if isinstance(service, ToolResult):
            return service
        path = await resolve_path(ctx, service)
        if path is None:
            return ToolResult(ok=True, output="这个会话本来就没绑学习路径，不用脱离。")
        await service.unbind_session(path.id)
        return ToolResult(
            ok=True,
            output=(
                f"已脱离路径《{path.title}》（进度、题目、作答都留着，随时可以 "
                "mastery_switch 回来）。接下来按普通问答继续；想学别的主题就 mastery_build 新建。"
            ),
            detail={"path_id": path.id, "left": True},
        )


__all__ = ["MasteryPathsTool", "MasterySwitchTool", "MasteryLeaveTool"]
