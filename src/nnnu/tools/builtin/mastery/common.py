"""mastery 八工具的公共件：路径解析、目标节点校验、脱敏序列化、人读文案。

**答案不外泄的三条约束**（改这个包前先读一遍）：
① 进提示词的只有**已作答过**的题（题干+答案+解析，复习必需），未作答的新题不进提示词；
② `mastery_status` / `mastery_paths` 只给数字与标题；
③ 答案键只在**本题判分之后**随反馈出现（`question_detail(..., reveal=...)`）。

第 ④ 条（旧版有、本版没了）：出题权归模型后，模型自己就知道它写的答案——
所以「答案键不进提示词」不再靠拦题干，而是靠**判分只认服务端落库的那一列**
（`learning_interactions.user_answer`，见 runtime 的暂停缝），模型转述的作答无效。

§16.5：本模块自研，上游 `deeptutor/capabilities/mastery` 只作对照阅读，不复制实现。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from nnnu.core.tool_protocol import ToolContext, ToolResult
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.models import (
    LearningInteraction,
    LearningNode,
    LearningPath,
    NextTarget,
    PathDetail,
)
from nnnu.services.learning.policy import gate_of, is_cleared
from nnnu.services.learning.service import LearningService
from nnnu.services.question_bank.models import Question

AskFn = Callable[..., Awaitable[str]]

STEM_PREVIEW_CHARS = 160
MAX_NODES = 40

# 学习者原话在 ctx.metadata 上的落脚点：`ask_user` 缝（capabilities/mastery/ask_seam.py）
# 收到答复后压一份，`mastery_assess` 取它当原话（定性门没有登记过的题，只此一路）。
ANSWER_KEY = "mastery_last_answer"


def preview(text: str, limit: int = STEM_PREVIEW_CHARS) -> str:
    plain = " ".join(str(text).split())
    return plain if len(plain) <= limit else plain[:limit] + "…"


def tracker_of(ctx: ToolContext) -> CostTracker | None:
    return ctx.metadata.get("cost_tracker")


async def path_for(
    service: LearningService, session_id: str | None, config_path_id: str = ""
) -> LearningPath | None:
    """定路径：**本会话绑定的优先**，config 兜底。

    顺序不能反。`ctx.config` 是**回合开始时**的快照，而 `mastery_switch` 是回合中改的
    会话绑定——config 优先的话，同一回合里刚切过去就被旧快照拽回来，「切了等于没切」。
    会话绑定在能力 `run()` 里已经对齐过（config 指名的路径第一件事就是 bind_session），
    所以会话优先不会漏掉 config ——只有会话真没绑上时才回落到它。
    """
    bound = await service.get_path_by_session(session_id) if session_id else None
    if bound is not None:
        return bound
    path_id = str(config_path_id or "").strip()
    if path_id:
        return await service.get_path_model(path_id)
    return None


async def resolve_path(ctx: ToolContext, service: LearningService) -> LearningPath | None:
    """`path_for` 的工具侧入口（模型不经手 path_id；切换走 `mastery_switch`）。"""
    return await path_for(service, ctx.session_id, str(ctx.config.get("path_id") or ""))


async def no_path(ctx: ToolContext, service: LearningService) -> ToolResult:
    """没有可用路径：把库里已有的列出来（模型看不到路径表，只能靠这里给）。"""
    lines: list[str] = []
    if ctx.session_id:
        lines.append("这个会话还没绑学习路径。")
    else:
        lines.append("这个会话没有可用的学习路径（没有会话上下文）。")
    summaries = await service.list_paths()
    if summaries:
        lines.append("库里已有的路径（对得上就 mastery_switch path_id=...）：")
        for summary in summaries[:10]:
            lines.append(
                f"- 《{summary.path.title}》：已过门 {summary.stats.mastered}/"
                f"{summary.stats.total}，下一目标 {summary.next_title or '—'}；id={summary.path.id}"
            )
    lines.append("都不合适就 mastery_build 新建一条（给 topic 与 nodes）。")
    return ToolResult(ok=False, output="\n".join(lines))


async def target_node(
    ctx: ToolContext,
    service: LearningService,
    detail: PathDetail,
    *,
    kind: str,
) -> tuple[LearningNode | None, ToolResult | None]:
    """定目标节点（参数优先，其次服务端现算的下一目标），并校验动作适配。

    `kind` 是 `quiz`（定量：做题到过门）或 `assess`（定性：讲一遍判定），
    两套门互不通用——定量节点讲课后的检查走 quiz，定性节点走 assess。
    """
    node_id = str(ctx.args.get("node_id") or "").strip()
    target: NextTarget = detail.next_target
    node = detail.node(node_id) if node_id else detail.node(target.node_id)
    if node is None:
        if target.done:
            return None, ToolResult(
                ok=True,
                output=get_prompt_manager()
                .render("mastery", ctx.language, "path_complete")
                .strip(),
                detail={"path_id": detail.path.id, "next": target.model_dump()},
            )
        return None, ToolResult(ok=False, output=f"节点 {node_id or target.node_id} 不在本路径里。")
    gate = gate_of(node.node_type)
    if kind == "assess" and gate is not None:
        return None, ToolResult(
            ok=False,
            output=f"「{node.title}」是定量节点（门 {gate:.0f} 分），过门看做题：用 mastery_quiz。",
        )
    if kind == "quiz" and gate is None:
        return None, ToolResult(
            ok=False,
            output=(
                f"「{node.title}」是定性节点，没有做题这一步："
                "用 mastery_assess 让它用自己的话讲一遍。"
            ),
        )
    return node, None


def question_detail(question: Question, *, reveal: str | None = None) -> dict[str, Any]:
    """题面 + 选项；`reveal` 只在该题**判分之后**传（答案键不外泄，见模块头 ③）。"""
    payload: dict[str, Any] = {
        "id": question.id,
        "stem": question.stem,
        "type": question.type,
        "options": question.options,
    }
    if reveal is not None:
        payload["answer"] = reveal
        payload["explanation"] = question.explanation or ""
    return payload


def replay_card(interaction: LearningInteraction) -> dict[str, Any]:
    """已判过的题回放成一张结果卡（幂等出口用）。"""
    return {
        "question": {"id": interaction.question_id, "stem": interaction.card_prompt},
        "interaction_id": interaction.id,
        "answer": interaction.user_answer,
        "answered": True,
        "grading": {
            "correct": interaction.correct,
            "score": interaction.score,
            "feedback": interaction.feedback,
            "source": interaction.grade_source,
        },
    }


def card_line(ctx: ToolContext, card: dict[str, Any], *, index: int, total: int) -> str:
    """一张卡的人读文本（结果卡本体走 detail，前端认 detail 里的结构）。"""
    question = card.get("question") or {}
    if not card.get("answered"):
        return (
            f"第 {index}/{total} 题：{preview(str(question.get('stem') or ''))}\n"
            f"{card.get('note') or ''}"
        )
    grading = card.get("grading") or {}
    parts = [
        f"第 {index}/{total} 题：{preview(str(question.get('stem') or ''))}",
        f"用户作答：{card.get('answer') or '(空)'}",
        f"判定：{'正确' if grading.get('correct') else '错误'}"
        f"（{float(grading.get('score') or 0.0):.2f}）",
    ]
    if grading.get("feedback"):
        parts.append(f"反馈：{grading['feedback']}")
    if question.get("answer"):
        parts.append(f"参考答案：{question['answer']}")
    if card.get("mastery") is not None:
        gate = card.get("gate")
        mastery_line = f"本节点掌握度：{float(card['mastery']):.1f}"
        if gate is not None:
            mastery_line += f"/{float(gate):.0f}"
        mastery_line += "（已过门）" if card.get("cleared") else "（未过门）"
        parts.append(mastery_line)
    return "\n".join(parts)


def action_label(prompts: Any, language: str, action: str | None) -> str:
    if not action:
        return "—"
    return prompts.render("mastery", language, f"state_block.action_{action}").strip() or action


def tree_lines(nodes: list[LearningNode]) -> str:
    lines = []
    for node in nodes:
        gate = gate_of(node.node_type)
        gate_text = f"门 {gate:.0f} 分" if gate is not None else "定性门"
        lines.append(f"{'  ' * node.depth}- {node.title}（{gate_text}）")
    return "\n".join(lines)


def status_lines(ctx: ToolContext, detail: PathDetail | None) -> str:
    """人读文本：路径地图 + 下一目标 + 薄弱点 + 复习安排（细节都在 detail 里）。"""
    if detail is None:
        return "学习路径不存在。"
    prompts = get_prompt_manager()
    path = detail.path
    stats = detail.stats
    target = detail.next_target
    lines = [
        prompts.render(
            "mastery",
            ctx.language,
            "state_block.path",
            title=path.title,
            total=stats.total,
            mastered=stats.mastered,
            due=stats.due,
            progress=f"{stats.progress:.0%}",
            id=path.id,
        ).strip(),
        prompts.render(
            "mastery",
            ctx.language,
            "state_block.next",
            reason=target.reason,
            action=action_label(prompts, ctx.language, target.action),
        ).strip(),
    ]
    for node in detail.nodes:
        gate = gate_of(node.node_type)
        marks = [
            "已过门"
            if is_cleared(node)
            else ("未开始" if node.last_practiced_at is None else "学习中"),
            f"掌握度 {node.mastery:.1f}" + (f"/{gate:.0f}" if gate is not None else "（定性）"),
        ]
        if node.next_review_at is not None and is_cleared(node):
            marks.append("到期待复习" if node.due else "已排复习")
        if node.id == target.node_id:
            marks.append("← 下一目标")
        lines.append(f"{'  ' * node.depth}- {node.title}（{' · '.join(marks)}；id={node.id}）")
    if detail.weak_points:
        lines.append("薄弱点（作答过但没过门）：")
        for weak in detail.weak_points[:5]:
            gap = (
                f"{weak.mastery:.1f}/{weak.gate:.0f}，还差 {weak.gap:.1f} 分"
                if weak.gate
                else f"定性没过（展示分 {weak.mastery:.1f}），需要重讲重评"
            )
            lines.append(f"- {weak.title}：{gap}（作答 {weak.attempted} 题，错过 {weak.wrong} 次）")
    if detail.reviews:
        lines.append("复习安排：")
        for review in detail.reviews[:5]:
            when = "已到期" if review.overdue else "未到期"
            lines.append(f"- {review.title}：{when}（第 {review.review_stage + 1} 档）")
    return "\n".join(lines)


def progress_payload(
    detail: PathDetail | None, node: LearningNode | None, *, action: str
) -> dict[str, Any]:
    """结果卡/状态卡的公共 detail 尾巴：节点 + 汇总 + 下一目标。"""
    payload: dict[str, Any] = {"action": action}
    if detail is not None:
        payload["path_id"] = detail.path.id
        payload["stats"] = detail.stats.model_dump()
        payload["next"] = detail.next_target.model_dump()
    if node is not None:
        payload["node"] = node.model_dump()
        payload["mastery"] = node.mastery
        payload["gate"] = gate_of(node.node_type)
        payload["gate_kind"] = node.gate_kind
        payload["cleared"] = is_cleared(node)
    return payload
