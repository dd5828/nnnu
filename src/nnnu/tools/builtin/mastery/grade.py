"""`mastery_grade`：拿**服务端落库的学习者原话**判分（三段式的第三段）。

判分只认 `learning_interactions.user_answer` 那一列——它在 `ask_user` 的暂停缝里落库
（见 `runtime/turn_runtime.py`），落库那一刻模型还没看见它，之后也改不动。所以模型
转述的作答一律无效：`answer` 参数只是**兜底**（卡超时、回合被停这些没走暂停缝的情况），
只要库里有原话就以库里为准。

判定是确定性的（`grade_with_fallback`）：选择题比对标签，简答先归一化/Jaccard、
没把握才请 LLM 判分器兜底。选项读不出时**拒绝判分**（判错会冤枉人，判对更糟）。
"""

from __future__ import annotations

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.learning.grading import grade_with_fallback, resolve_choice_submission
from nnnu.services.learning.models import LearningInteraction
from nnnu.services.learning.service import LearningError, LearningService, get_learning_service
from nnnu.services.question_bank.service import get_question_bank
from nnnu.tools.builtin.mastery.common import (
    card_line,
    no_path,
    progress_payload,
    question_detail,
    replay_card,
    resolve_path,
    tracker_of,
)
from nnnu.tools.builtin.mastery.quiz import CHOICE_TYPES


class MasteryGradeTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_grade",
        description="tools.mastery_grade",
        parameters={
            "type": "object",
            "properties": {
                "question_id": {
                    "type": "string",
                    "description": "mastery_quiz 给的 question_id；不填就判当前未决的那道",
                },
                "answer": {
                    "type": "string",
                    "description": (
                        "只在服务端没收到作答时兜底（卡超时、回合被停）；库里有原话时这个参数被忽略"
                    ),
                },
            },
            "required": [],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.mastery_grade",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        path = await resolve_path(ctx, service)
        if path is None:
            return await no_path(ctx, service)

        interaction = await self._target_interaction(ctx, service, path.id)
        if interaction is None:
            return ToolResult(
                ok=False,
                output="现在没有未决的题：用 mastery_quiz 出新题，或 mastery_status 看下一目标。",
            )
        if interaction.status != "awaiting_input":
            return ToolResult(
                ok=True,
                output=(
                    f"这道题已经判过了（{interaction.user_answer!r} → "
                    f"{'正确' if interaction.correct else '错误'}：{interaction.feedback}）"
                ),
                detail={
                    "action": "grade",
                    "path_id": path.id,
                    "cards": [replay_card(interaction)],
                    "replayed": True,
                },
            )

        if interaction.kind != "quiz" or not interaction.question_id:
            # 定性门的卡（让学习者用自己的话讲一遍）不是判分题：这儿判不了，
            # 去 mastery_assess 记结果。**不能**当「题被删了」作废掉——那张卡还得用
            return ToolResult(
                ok=False,
                output=(
                    "这张卡是让用户讲一遍的（定性门），不是判分题：用 mastery_assess "
                    "记录他讲得怎么样，别用 mastery_grade。"
                ),
            )

        # 服务端落库的原话优先；落了就不许模型转述覆盖
        answer = (interaction.user_answer or "").strip()
        if not answer:
            answer = str(ctx.args.get("answer") or "").strip()
        if not answer:
            return ToolResult(
                ok=False,
                output=(
                    "服务端还没收到这一题的作答：先用 ask_user 把卡发出去让用户答，"
                    "或让用户把答案发过来再调一次（那时把答案放 answer 参数里）。"
                ),
            )

        question = await get_question_bank().get_question(interaction.question_id)
        if question is None:
            await service.close_interaction(
                interaction.id,
                user_answer=answer,
                correct=None,
                score=None,
                feedback="题目已被删除，无法判分",
                grade_source="unreadable",
                status="abandoned",
            )
            return ToolResult(ok=False, output="那道题已经被删掉了，这一题作废（不记入掌握度）。")

        submission = answer
        if question.type in CHOICE_TYPES:
            submission = resolve_choice_submission(answer, question.options)
            if not submission:
                return ToolResult(
                    ok=False,
                    output=(
                        "没能读出用户选的是哪个选项，这一题先不判（不记入掌握度）："
                        "让用户直接给选项标签（如 B），再调一次。"
                    ),
                )

        result, feedback = await grade_with_fallback(
            question, submission, language=ctx.language, tracker=tracker_of(ctx)
        )
        await get_question_bank().record_attempt(
            question.id,
            answer=submission,
            correct=result.correct,
            score=result.score,
            source=result.source,
            feedback=feedback,
            session_id=ctx.session_id or None,
        )
        updated = await service.on_attempt(interaction.node_id)
        try:
            await service.close_interaction(
                interaction.id,
                user_answer=submission,
                correct=result.correct,
                score=result.score,
                feedback=feedback,
                grade_source=result.source,
                session_id=ctx.session_id or None,
                turn_id=ctx.turn_id,
            )
        except LearningError as exc:
            return ToolResult(ok=False, output=str(exc))

        detail = await service.get_path(path.id)
        payload = progress_payload(detail, updated, action="grade")
        card = {
            "question": question_detail(question, reveal=question.answer),
            "interaction_id": interaction.id,
            "answer": submission,
            "answered": True,
            "grading": {
                "correct": result.correct,
                "score": result.score,
                "feedback": feedback,
                "source": result.source,
            },
            "mastery": payload.get("mastery", 0.0),
            "cleared": payload.get("cleared", False),
            "gate": payload.get("gate"),
        }
        payload["cards"] = [card]
        return ToolResult(ok=True, output=card_line(ctx, card, index=1, total=1), detail=payload)

    async def _target_interaction(
        self, ctx: ToolContext, service: LearningService, path_id: str
    ) -> LearningInteraction | None:
        """定要判的那道题：指定 question_id 优先（含已判过的，好回放），其次本路径未决的那道。"""
        question_id = str(ctx.args.get("question_id") or "").strip()
        pending = await service.pending_interaction(path_id)
        if not question_id:
            return pending
        if pending is not None and pending.question_id == question_id:
            return pending
        return await service.interaction_by_question(path_id, question_id)
