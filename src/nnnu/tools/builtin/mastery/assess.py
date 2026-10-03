"""`mastery_assess`：定性门（concept / design）——模型听完学习者自己讲的一段话，判过不过。

**判定权归模型**：不再有独立的 LLM 评定器。模型是老师，讲没讲清楚它自己听得出；
服务端只把它落库（`assess_passed` + `assessed_at`），并在过门后重排复习阶梯。

判定要严：宁可让人再讲一遍，也不能把没学会的放过去（定性门没有分数可以兜）。
"""

from __future__ import annotations

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.learning.service import LearningError, get_learning_service
from nnnu.tools.builtin.mastery.common import (
    ANSWER_KEY,
    no_path,
    progress_payload,
    resolve_path,
    target_node,
)


class MasteryAssessTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_assess",
        description="tools.mastery_assess",
        parameters={
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "目标知识点 id（取自 mastery_status；不填就是它给的下一目标）",
                },
                "passed": {
                    "type": "boolean",
                    "description": "学习者这段讲解算不算讲清楚了（判断要严，拿不准就 false）",
                },
                "answer": {
                    "type": "string",
                    "description": "学习者的原话（原样记进交互历史，供以后回顾）",
                },
                "feedback": {"type": "string", "description": "给学习者的反馈（可空）"},
            },
            "required": ["passed"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.mastery_assess",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not isinstance(ctx.args.get("passed"), bool):
            return ToolResult(ok=False, output="需要 passed（布尔）：这一遍讲解过没过。")
        passed = bool(ctx.args["passed"])
        feedback = str(ctx.args.get("feedback") or "").strip()
        # 学习者的**原话**优先（ask_user 缝压在 metadata 上的那份），参数只兜底：
        # 定性门没有登记过的题，原话不进库就只剩模型转述了
        answer = str(ctx.metadata.get(ANSWER_KEY) or "").strip()
        if not answer:
            answer = str(ctx.args.get("answer") or "").strip()

        path = await resolve_path(ctx, service)
        if path is None:
            return await no_path(ctx, service)
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        node, problem = await target_node(ctx, service, detail, kind="assess")
        if problem is not None:
            return problem
        assert node is not None

        interaction = await service.pending_interaction(path.id)
        if interaction is not None and interaction.node_id != node.id:
            # 换节点了：旧卡作废，免得下一轮又被当成「未决的那道」提出来
            await service.abandon_pending(path.id)
            interaction = None
        if interaction is None:
            interaction = await service.open_interaction(
                path_id=path.id,
                node_id=node.id,
                kind="assess",
                card_prompt=node.title,
                session_id=ctx.session_id or None,
                turn_id=ctx.turn_id,
            )
        try:
            updated = await service.record_qualitative(node.id, passed=passed)
        except LearningError as exc:
            return ToolResult(ok=False, output=str(exc))
        await service.close_interaction(
            interaction.id,
            user_answer=answer,
            correct=passed,
            score=1.0 if passed else 0.0,
            feedback=feedback,
            grade_source="model",
            session_id=ctx.session_id or None,
            turn_id=ctx.turn_id,
        )

        detail = await service.get_path(path.id)
        payload = progress_payload(detail, updated or node, action="assess")
        card = {
            "question": {"id": "", "stem": node.title, "type": "assess", "options": []},
            "interaction_id": interaction.id,
            "answer": answer,
            "answered": True,
            "grading": {
                "correct": passed,
                "score": 1.0 if passed else 0.0,
                "feedback": feedback,
                "source": "model",
            },
            "mastery": payload.get("mastery", 0.0),
            "cleared": payload.get("cleared", False),
            "gate": None,
        }
        payload["cards"] = [card]
        verdict = "讲清楚了，这一节点算过关" if passed else "还差点意思：这一节点没过"
        lines = [
            f"节点「{node.title}」的定性评定：{verdict}。",
            feedback,
            f"下一目标：{detail.next_target.reason if detail else '—'}",
        ]
        return ToolResult(
            ok=True,
            output="\n".join(line for line in lines if line),
            detail=payload,
        )
