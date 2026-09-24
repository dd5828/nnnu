"""question_bank 工具（§7.2）：查题库 / 往题库里加题，模型在聊天里自主调用。

两个动作：
- search：按筛选（全部 / 错题 / 未作答）+ 知识点 + 标签 + 关键词查题，命中渲染成
  编号列表给模型读（题面截断），结构化数据走 detail 给前端展示（§6.3）；
- add：把模型整理好的题目写进题库（结构校验在服务层，校验不过原样告诉模型）。

入库的来源标 `tool`、会话说 `ctx.session_id`，方便日后区分「聊天里手动加」与
「出题能力批量生成」。工具层吞异常是惯例：失败也返回 ok=False 的文本让模型自己圆场。
"""

from typing import Any, cast

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.question_bank.models import Question
from nnnu.services.question_bank.service import (
    ListFilter,
    QuestionBankError,
    QuestionBankService,
    get_question_bank,
)

STEM_PREVIEW_CHARS = 120
MAX_LIMIT = 20
DEFAULT_LIMIT = 10
TYPE_LABELS = {"single": "单选", "multi": "多选", "short": "简答"}
FILTER_LABELS: dict[str, str] = {"all": "全部", "wrong": "错题", "unanswered": "未作答"}

_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "stem": {"type": "string", "description": "题面"},
        "type": {
            "type": "string",
            "enum": ["single", "multi", "short"],
            "default": "single",
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "选择题选项（简答题留空）",
        },
        "answer": {
            "type": "string",
            "description": "单选取一个标签（如 B）；多选取标签升序拼接（如 AC）；简答写参考答案",
        },
        "explanation": {"type": "string", "description": "解析"},
        "knowledge_point": {"type": "string"},
        "difficulty": {
            "type": "string",
            "enum": ["easy", "medium", "hard"],
            "default": "medium",
        },
    },
    "required": ["stem", "answer"],
}


class QuestionBankTool(BaseTool):
    definition = ToolDefinition(
        name="question_bank",
        description="tools.question_bank",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "add"], "default": "search"},
                "query": {"type": "string", "description": "search：题面或解析的关键词"},
                "filter": {
                    "type": "string",
                    "enum": ["all", "wrong", "unanswered"],
                    "default": "all",
                },
                "knowledge_point": {"type": "string"},
                "tag": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
                "questions": {
                    "type": "array",
                    "items": _QUESTION_SCHEMA,
                    "description": "add：要入库的题目",
                },
            },
            "required": ["action"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        action = str(ctx.args.get("action") or "search")
        try:
            service = get_question_bank()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        if action == "add":
            return await self._add(ctx, service)
        if action != "search":
            return ToolResult(ok=False, output=f"未知动作 {action!r}，只支持 search / add。")
        return await self._search(ctx, service)

    async def _search(self, ctx: ToolContext, service: QuestionBankService) -> ToolResult:
        wanted = str(ctx.args.get("filter") or "all")
        filter_ = cast(ListFilter, wanted) if wanted in FILTER_LABELS else "all"  # 白名单已校验
        try:
            questions = await service.list_questions(
                filter=filter_,
                knowledge_point=_text(ctx.args.get("knowledge_point")),
                tag=_text(ctx.args.get("tag")),
                search=_text(ctx.args.get("query")),
                limit=_clamp(ctx.args.get("limit")),
            )
        except QuestionBankError as exc:
            return ToolResult(ok=False, output=f"查询参数不对：{exc}")

        label = FILTER_LABELS[filter_]
        if not questions:
            return ToolResult(ok=True, output=f"题库里没有符合条件的题目（筛选：{label}）。")

        lines = [f"题库命中 {len(questions)} 道（筛选：{label}）："]
        for index, question in enumerate(questions, start=1):
            marks = [TYPE_LABELS.get(question.type, question.type)]
            if question.knowledge_point:
                marks.append(question.knowledge_point)
            if question.wrong_count:
                marks.append(f"错过 {question.wrong_count} 次")
            if question.last_attempt_at is None:
                marks.append("未作答")
            else:
                marks.append(f"掌握度 {question.mastery:.0%}")
            lines.append(
                f"{index}. [{' · '.join(marks)}] {_preview(question.stem)}（id={question.id}）"
            )
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            detail={"questions": [_brief(question) for question in questions]},
        )

    async def _add(self, ctx: ToolContext, service: QuestionBankService) -> ToolResult:
        raw = ctx.args.get("questions")
        if not isinstance(raw, list) or not raw:
            return ToolResult(ok=False, output="add 需要带上 questions 数组（至少一道题）。")
        rows = [
            {**item, "source": "tool", "session_id": ctx.session_id}
            for item in raw
            if isinstance(item, dict)
        ]
        if len(rows) != len(raw):
            return ToolResult(ok=False, output="questions 里每一项都得是题目对象。")
        try:
            questions = await service.add_questions(rows)
        except QuestionBankError as exc:
            return ToolResult(ok=False, output=f"入库失败：{exc}")
        ids = "、".join(question.id for question in questions)
        return ToolResult(
            ok=True,
            output=f"已入库 {len(questions)} 道题：{ids}",
            detail={"questions": [_brief(q) for q in questions]},
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _clamp(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(number, MAX_LIMIT))


def _preview(stem: str) -> str:
    text = " ".join(stem.split())
    return text if len(text) <= STEM_PREVIEW_CHARS else text[:STEM_PREVIEW_CHARS] + "…"


def _brief(question: Question) -> dict[str, Any]:
    """给前端展示用的精简字段（题干全文在这没必要，题库页自己会拉）。"""
    return {
        "id": question.id,
        "stem": question.stem,
        "type": question.type,
        "knowledge_point": question.knowledge_point,
        "difficulty": question.difficulty,
        "mastery": question.mastery,
        "wrong_count": question.wrong_count,
    }
