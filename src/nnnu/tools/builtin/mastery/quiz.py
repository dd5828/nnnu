"""`mastery_quiz`：模型现场出题 → 登记入库（**答案留服务端**）→ 回一个能直接发卡的 payload。

这是「出题权归模型」的落点。三段式里的第一段，**不阻塞**：
登记完就把脱敏后的题面交回模型，由模型自己调 `ask_user` 把卡发出去（第二段），
用户答完再调 `mastery_grade` 判分（第三段）。

**为什么答案不回给模型**：模型当然知道自己写了什么答案，但把它再回灌一遍没有好处——
`mastery_grade` 只认服务端落库的那一列，模型转述的作答一律无效（见 runtime 暂停缝）。
payload 里因此只有 `question_id` / 题面 / 选项 / 题型，`expected_answer` 与
`explanation` 落在库里等判分那一刻再出现。
"""

from __future__ import annotations

import json
from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.grading import resolve_expected_key
from nnnu.services.learning.service import LearningError, get_learning_service
from nnnu.services.question_bank.service import QuestionBankError, get_question_bank
from nnnu.tools.builtin.mastery.common import no_path, resolve_path, target_node

CHOICE_TYPES = ("single", "multi")
QUESTION_TYPES = ("single", "multi", "short")
DIFFICULTIES = ("easy", "medium", "hard")


class MasteryQuizTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_quiz",
        description="tools.mastery_quiz",
        parameters={
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "目标知识点 id（取自 mastery_status；不填就是它给的下一目标）",
                },
                "question": {"type": "string", "description": "题干"},
                "expected_answer": {
                    "type": "string",
                    "description": (
                        "正确答案。选择题给选项标签（如 B、AC）或选项原文，"
                        "简答题给参考答案文本——它只留在服务端，不会回给模型"
                    ),
                },
                "question_type": {
                    "type": "string",
                    "enum": list(QUESTION_TYPES),
                    "default": "single",
                    "description": "single 单选 / multi 多选 / short 简答",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "选项原文数组（single/multi 必填，至少两个；short 不填）",
                },
                "explanation": {"type": "string", "description": "解析（判分后随反馈出现）"},
                "difficulty": {
                    "type": "string",
                    "enum": list(DIFFICULTIES),
                    "default": "medium",
                },
            },
            "required": ["question", "expected_answer"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.mastery_quiz",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        stem = str(ctx.args.get("question") or "").strip()
        expected = str(ctx.args.get("expected_answer") or "").strip()
        if not stem or not expected:
            return ToolResult(ok=False, output="需要 question（题干）与 expected_answer（答案）。")
        question_type = str(ctx.args.get("question_type") or "single").strip()
        if question_type not in QUESTION_TYPES:
            return ToolResult(
                ok=False, output=f"question_type 只支持 {'、'.join(QUESTION_TYPES)}。"
            )
        options = _clean_options(ctx.args.get("options"))
        if question_type in CHOICE_TYPES and len(options) < 2:
            return ToolResult(ok=False, output="选择题至少要给两个选项（options）。")
        if question_type == "short" and options:
            return ToolResult(ok=False, output="简答题不要给 options。")

        path = await resolve_path(ctx, service)
        if path is None:
            return await no_path(ctx, service)
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        node, problem = await target_node(ctx, service, detail, kind="quiz")
        if problem is not None:
            return problem
        assert node is not None

        # 答案键按入库口径归一（标签串）；读不出就不登记，免得定错答案键
        if question_type in CHOICE_TYPES:
            key = resolve_expected_key(expected, options, multiple=question_type == "multi")
            if not key:
                return ToolResult(
                    ok=False,
                    output=(
                        "从 expected_answer 里读不出唯一答案：选择题请给选项标签"
                        f"（{'、'.join('ABCDEFGH'[: len(options)])}，多选如 AC）或与选项原文完全一致的文本。"
                    ),
                )
        else:
            key = expected

        try:
            question = await get_question_bank().create_question(
                stem=stem,
                answer=key,
                options=options,
                type=question_type,
                explanation=str(ctx.args.get("explanation") or "").strip() or None,
                knowledge_point=node.title,
                difficulty=str(ctx.args.get("difficulty") or "medium").strip(),
                source="mastery",
                session_id=ctx.session_id or None,
                node_id=node.id,
            )
        except QuestionBankError as exc:
            return ToolResult(ok=False, output=f"题目入库失败：{exc}")

        try:
            interaction = await service.open_interaction(
                path_id=path.id,
                node_id=node.id,
                question_id=question.id,
                kind="quiz",
                card_prompt=stem,
                session_id=ctx.session_id or None,
                turn_id=ctx.turn_id,
            )
        except LearningError as exc:
            return ToolResult(ok=False, output=str(exc))

        prompts = get_prompt_manager()
        context = prompts.render(
            "mastery", ctx.language, "quiz.card_context", title=node.title
        ).strip()
        payload = _ask_payload(question.id, stem, question_type, options, context)
        return ToolResult(
            ok=True,
            output=(
                f"已登记一道题（question_id={question.id}，节点「{node.title}」）。\n"
                "现在调 ask_user 把它发出去：question / options / context 三项"
                "**原样照抄**下面的值（题型不同 options 可能是空数组，空就照空给）：\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
                f"用户答完后调 mastery_grade（question_id={question.id}）判分——"
                "作答以服务端收到的那份为准，不用你转述。"
            ),
            detail={
                "action": "quiz",
                "status": "registered",
                "path_id": path.id,
                "node": node.model_dump(),
                "question_id": question.id,
                "interaction_id": interaction.id,
                "question_type": question_type,
                "question": stem,
                "options": options,
                "ask_user": payload,
                "with_context": context,
            },
        )


def _clean_options(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _ask_payload(
    question_id: str, stem: str, question_type: str, options: list[str], context: str
) -> dict[str, Any]:
    """模型转手交给 `ask_user` 的形状——就是 `ask_user` 的 question / options / context。

    选项正文放在 `description`：卡片把 `label` 显示成加粗的 A/B/C，正文另起一行
    （见 web/components/chat/ActiveTurnView.tsx 的 AskUserCard）。
    """
    return {
        "question": stem,
        "options": [
            {"label": label, "description": body} for label, body in zip("ABCDEFGH", options)
        ]
        if question_type in CHOICE_TYPES
        else [],
        "context": context,
        "question_id": question_id,
    }
