"""mastery 工具（§7.5 重做）：学习路径的读、建、切、脱离，以及**回合内答题闭环**。

九个动作：

| 动作 | 干什么 | LLM |
|---|---|---|
| `status` | 只读：路径地图 + 下一目标 + 未决题 + 到期复习 | 0 |
| `paths` | 列全部路径（进度 / 到期数 / 下一目标） | 0 |
| `build` | 建树（同主题且本会话正绑着它 → 原样返回；同一主题允许多条路径） | 0 |
| `switch` | 把本会话绑到另一条路径（进度都在） | 0 |
| `leave` | 脱离当前路径（进度/题目/作答全留） | 0 |
| `quiz` | 出题 → 出卡 → 收答复 → 判分 → 即时反馈（**阻塞**） | 缺题补一次 + 简答兜底 |
| `probe` | 摸底：只对「从未碰过」的定量节点，1–2 题过了就跳过讲解 | 同上 |
| `assess` | 定性门：让用户用自己的话讲一遍，LLM 判过不过（**阻塞**） | 1 |
| `grade` | 收尾未决题（卡超时/回合被停后的唯一出口），幂等 | 简答兜底 |

**为什么判分必须在一个工具调用里闭环**：本仓 `run_agent_loop` 的工具实参只来自模型
JSON，没有服务端给工具注入实参的钩子；循环里唯一的暂停缝是 `ctx.metadata["ask_user_fn"]`
（`LoopDeps.ask_user` 是死字段）。所以抄不了上游「模型再调一次 ask_user、管道重绑卡片」——
模型一旦自己写卡，题干/选项/答案键就都过它的手了。

**答案不外泄的四条约束**（改本文件前先读一遍）：
① 卡片一律由服务端从题库行渲染，模型不写题干与选项；
② 进提示词的只有**已作答过**的题（题干+答案+解析，复习必需），未作答的新题不进提示词；
③ `status`/`paths` 只给数字与标题；
④ 答案键只在**本题判分之后**随反馈出现。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Awaitable, Callable

from nnnu.core.ids import new_id
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.grading import (
    assessor_from_settings,
    grade_with_fallback,
    resolve_choice_submission,
)
from nnnu.services.learning.mastery import compute_state
from nnnu.services.learning.models import (
    LearningInteraction,
    LearningNode,
    LearningPath,
    NextTarget,
    PathDetail,
)
from nnnu.services.learning.policy import gate_of, is_cleared
from nnnu.services.learning.quiz import (
    AUTHOR_DEFAULT,
    clamp_count,
    ensure_questions,
    node_labels,
    pick_next_question,
)
from nnnu.services.learning.service import LearningError, LearningService, get_learning_service
from nnnu.services.llm.errors import LLMError
from nnnu.services.question_bank.models import Question
from nnnu.services.question_bank.service import get_question_bank

AskFn = Callable[..., Awaitable[str]]

STEM_PREVIEW_CHARS = 160
MAX_NODES = 40
MAX_CARDS = 3
MAX_UNREADABLE_RETRY = 1  # 选项读不出时重问的次数（计划里写死 1 次）

ACTIONS = (
    "status",
    "build",
    "paths",
    "switch",
    "leave",
    "quiz",
    "probe",
    "assess",
    "grade",
)
CHOICE_TYPES = ("single", "multi")

_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "节点标题（一句话能说清的知识点）"},
        "type": {
            "type": "string",
            "enum": ["memory", "procedure", "concept", "design"],
            "default": "concept",
            "description": (
                "memory 记忆 / procedure 操作 / concept 概念 / design 设计——"
                "前两类靠做题到 90 分过关，后两类靠用户自己讲一遍判定过关"
            ),
        },
        "description": {"type": "string", "description": "这个节点要学什么（可选）"},
        "parent_index": {
            "type": "integer",
            "description": "父节点在 nodes 数组里的下标（从 0 起，必须小于自己的下标）；顶层节点不填",
        },
        "parent_title": {
            "type": "string",
            "description": "父节点标题（必须在这个节点之前出现过）；顶层节点不填",
        },
    },
    "required": ["title"],
}


class MasteryTool(BaseTool):
    definition = ToolDefinition(
        name="mastery",
        description="tools.mastery",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "default": "status",
                },
                "topic": {"type": "string", "description": "build：学习主题"},
                "title": {"type": "string", "description": "build：路径标题（不给就用主题）"},
                "summary": {"type": "string", "description": "build：一句话说明这条路径"},
                "nodes": {
                    "type": "array",
                    "items": _NODE_SCHEMA,
                    "description": "build：节点数组，**顺序即学习顺序**，子节点紧跟父节点",
                },
                "path_id": {"type": "string", "description": "不填则用当前会话绑定的路径"},
                "node_id": {
                    "type": "string",
                    "description": "quiz/probe/assess：目标节点（默认下一目标）",
                },
                "question_id": {
                    "type": "string",
                    "description": "quiz/grade：指定题目（默认自动挑）",
                },
                "author": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": AUTHOR_DEFAULT,
                    "description": "quiz：这个节点至少要有几道题（不够才补）",
                },
                "cards": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_CARDS,
                    "default": 1,
                    "description": "quiz/probe：一次连出几张卡（连出时按顺序一题一卡）",
                },
                "answer": {"type": "string", "description": "grade：用户对未决那道题的作答"},
                "interaction_id": {
                    "type": "string",
                    "description": "grade：指定要收尾的交互（默认取未决的那道）",
                },
            },
            "required": ["action"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.mastery",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        action = str(ctx.args.get("action") or "status")
        if action not in ACTIONS:
            return ToolResult(
                ok=False, output=f"未知动作 {action!r}，只支持 {'、'.join(ACTIONS)}。"
            )
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        try:
            if action == "build":
                return await self._build(ctx, service)
            if action == "paths":
                return await self._paths(ctx, service)
            path = await self._resolve(ctx, service)
            if path is None:
                return await self._no_path(ctx, service)
            if action == "switch":
                return await self._switch(ctx, service, path)
            if action == "leave":
                return await self._leave(ctx, service, path)
            if action == "status":
                return await self._status(ctx, service, path)
            if action == "quiz":
                return await self._ask_cards(ctx, service, path, kind="quiz")
            if action == "probe":
                return await self._ask_cards(ctx, service, path, kind="probe")
            if action == "assess":
                return await self._assess(ctx, service, path)
            return await self._grade(ctx, service, path)
        except LearningError as exc:
            return ToolResult(ok=False, output=str(exc))
        except sqlite3.IntegrityError as exc:
            # 未决交互的部分唯一索引撞车（重试一次仍失败）：不把异常抛穿回合
            return ToolResult(ok=False, output=f"学习路径写入冲突（{exc}）：把这一步重做一次就好。")

    # ---- 只读与绑定 ----

    async def _status(
        self, ctx: ToolContext, service: LearningService, path: LearningPath
    ) -> ToolResult:
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        return ToolResult(
            ok=True,
            output=self._status_lines(ctx, detail),
            detail=detail.model_dump(),
        )

    async def _paths(self, ctx: ToolContext, service: LearningService) -> ToolResult:
        summaries = await service.list_paths()
        if not summaries:
            # 「一条路径都还没有」是正常状态，不是调用出错：给指路，别标成 ok=False
            return ToolResult(
                ok=True,
                output="还没有任何学习路径：用 action=build 建一条（给 topic 与 nodes）。",
                detail={"paths": []},
            )
        prompts = get_prompt_manager()
        lines = [prompts.render("mastery", ctx.language, "state_block.paths_intro").strip()]
        rows: list[dict[str, Any]] = []
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
                    action=self._action_label(prompts, ctx.language, summary.next_action),
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

    async def _switch(
        self, ctx: ToolContext, service: LearningService, path: LearningPath
    ) -> ToolResult:
        target_id = str(ctx.args.get("path_id") or "").strip()
        if not target_id:
            return ToolResult(
                ok=False, output="switch 需要 path_id（先用 action=paths 看有哪些）。"
            )
        if path.id != target_id:
            return ToolResult(ok=False, output=f"switch 的目标路径 {target_id} 不存在。")
        await service.bind_session(path.id, ctx.session_id)
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        return ToolResult(
            ok=True,
            output=f"已切到路径《{path.title}》：\n{self._status_lines(ctx, detail)}",
            detail=detail.model_dump(),
        )

    async def _leave(
        self, ctx: ToolContext, service: LearningService, path: LearningPath
    ) -> ToolResult:
        await service.unbind_session(path.id)
        return ToolResult(
            ok=True,
            output=(
                f"已脱离路径《{path.title}》（进度、题目、作答都留着，随时可以 switch 回来）。"
                "接下来按普通问答继续；想学别的主题就用 action=build 新建一条。"
            ),
            detail={"path_id": path.id, "left": True},
        )

    async def _build(self, ctx: ToolContext, service: LearningService) -> ToolResult:
        topic = str(ctx.args.get("topic") or "").strip()
        if not topic:
            return ToolResult(ok=False, output="build 需要 topic（学习主题）。")
        nodes = ctx.args.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return ToolResult(ok=False, output="build 需要 nodes 数组（至少一个节点）。")
        if len(nodes) > MAX_NODES:
            return ToolResult(ok=False, output=f"一条路径最多 {MAX_NODES} 个节点，请先拆小一点。")
        rows = [row for row in nodes if isinstance(row, dict)]
        if len(rows) != len(nodes):
            return ToolResult(ok=False, output="nodes 里每一项都得是节点对象。")

        # **窄幂等**：只有「本会话正绑着的这条」主题也对得上才原样返回（挡模型重试）。
        # 同一主题允许多条路径——用户想重学一遍就该能再建一条。
        bound = await service.get_path_by_session(ctx.session_id)
        if bound is not None and bound.topic.strip().casefold() == topic.casefold():
            detail = await service.get_path(bound.id)
            text = self._status_lines(ctx, detail) if detail else ""
            return ToolResult(
                ok=True,
                output=(
                    f"这条路径已经建好、也正绑在这个会话上（《{bound.title}》 {bound.id}），"
                    f"不重复建：\n{text}"
                ),
                detail={"path_id": bound.id, "created": False},
            )
        try:
            path, built = await service.create_path(
                topic=topic,
                title=str(ctx.args.get("title") or "").strip() or None,
                summary=str(ctx.args.get("summary") or "").strip() or None,
                nodes=rows,
                session_id=ctx.session_id or None,
            )
        except LearningError as exc:
            return ToolResult(ok=False, output=f"建路径失败：{exc}")
        detail = await service.get_path(path.id)
        target = detail.next_target if detail else None
        return ToolResult(
            ok=True,
            output=(
                f"已建好学习路径《{path.title}》（id={path.id}，共 {len(built)} 个节点，"
                f"顺序即学习顺序）：\n{self._tree_lines(built)}\n"
                f"下一目标：{target.reason if target else '—'}"
            ),
            detail={
                "path_id": path.id,
                "created": True,
                "nodes": [node.model_dump() for node in built],
                "next": target.model_dump() if target else None,
            },
        )

    # ---- 出卡判分闭环 ----

    async def _ask_cards(
        self,
        ctx: ToolContext,
        service: LearningService,
        path: LearningPath,
        *,
        kind: str,
    ) -> ToolResult:
        """`quiz` / `probe` 的公共实现：出题 → 一张一张出卡 → 判分 → 反馈。"""
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        node, problem = await self._target_node(ctx, service, detail, kind=kind)
        if problem is not None:
            return problem
        assert node is not None  # _target_node 成功时必然给了节点

        ask_fn = self._ask_fn(ctx)
        if ask_fn is None:
            return ToolResult(ok=False, output="当前环境不支持回合中途提问（无前端连接）。")

        strategy, type_label = node_labels(ctx.language, node)
        tracker = self._tracker(ctx)
        count = clamp_count(ctx.args.get("author") if kind == "quiz" else 2)
        cards = self._card_count(ctx, default=1 if kind == "quiz" else 2)
        added = await ensure_questions(
            node,
            count=count,
            language=ctx.language,
            strategy=strategy,
            node_type_label=type_label,
            session_id=ctx.session_id or None,
            tracker=tracker,
        )
        results: list[dict[str, Any]] = []
        asked: list[str] = []
        for index in range(cards):
            question = await self._next_question(ctx, node, asked)
            if question is None:
                break
            asked.append(question.id)
            outcome = await self._run_card(
                ctx,
                service,
                path,
                node,
                question,
                kind=kind,
                ask_fn=ask_fn,
                index=index + 1,
                total=cards,
                tracker=tracker,
            )
            results.append(outcome)
            if not outcome.get("answered"):
                break
        if not results:
            return ToolResult(
                ok=False,
                output=f"节点「{node.title}」现在没有可出的题，先讲解要点，再用 quiz 出题。",
            )
        return await self._cards_result(ctx, service, path, node, results, added=added, kind=kind)

    async def _run_card(
        self,
        ctx: ToolContext,
        service: LearningService,
        path: LearningPath,
        node: LearningNode,
        question: Question,
        *,
        kind: str,
        ask_fn: AskFn,
        index: int,
        total: int,
        tracker: CostTracker | None,
    ) -> dict[str, Any]:
        """一张卡：渲染 → 阻塞等答复 → 归一 → 判分 → 记作答 → 落交互。"""
        prompts = get_prompt_manager()
        options = [
            {"label": label, "description": option}
            for label, option in zip("ABCDEFGH", question.options)
        ]
        context = prompts.render(
            "mastery",
            ctx.language,
            "quiz.card_context",
            title=node.title,
            index=index,
            total=total,
        ).strip()
        interaction = await service.open_interaction(
            path_id=path.id,
            node_id=node.id,
            question_id=question.id,
            kind=kind,
            card_prompt=question.stem,
            session_id=ctx.session_id or None,
            turn_id=ctx.turn_id,
        )
        raw = await self._ask_answer(
            ask_fn, question=question.stem, options=options, context=context
        )
        if not raw:
            # 超时/空答复：不判不挂账（留着未决行，下一回合 answer_pending 优先重提）
            return {
                "question": self._question_detail(question),
                "interaction_id": interaction.id,
                "answer": "",
                "answered": False,
                "note": "用户没作答（超时或跳过），这一题留着下一轮再问",
            }
        submission = raw
        if question.type in CHOICE_TYPES:
            submission = resolve_choice_submission(raw, question.options)
            for _ in range(MAX_UNREADABLE_RETRY):
                if submission:
                    break
                again = await self._ask_answer(
                    ask_fn,
                    question=prompts.render("mastery", ctx.language, "quiz.ask_clarify").strip(),
                    options=options,
                    context=context,
                )
                if not again:
                    break
                raw = again
                submission = resolve_choice_submission(again, question.options)
            if not submission:
                # 读不出选项：**不判、不挂账**（判错会冤枉人，判对更糟）
                await service.close_interaction(
                    interaction.id,
                    user_answer=raw,
                    correct=None,
                    score=None,
                    feedback="没能读出用户选的是哪一个选项",
                    grade_source="unreadable",
                    status="abandoned",
                )
                return {
                    "question": self._question_detail(question),
                    "interaction_id": interaction.id,
                    "answer": raw,
                    "answered": False,
                    "note": "答复读不出选项，这一题没判分（不记入掌握度）",
                }
        result, feedback = await grade_with_fallback(
            question, submission, language=ctx.language, tracker=tracker
        )
        bank = get_question_bank()
        await bank.record_attempt(
            question.id,
            answer=submission,
            correct=result.correct,
            score=result.score,
            source=result.source,
            feedback=feedback,
            session_id=ctx.session_id or None,
        )
        # 掌握度回写（幂等：从作答序列重算）+ 交互落定
        updated = await service.on_attempt(node.id)
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
        return {
            "question": self._question_detail(question, reveal=question.answer),
            "interaction_id": interaction.id,
            "answer": submission,
            "answered": True,
            "grading": {
                "correct": result.correct,
                "score": result.score,
                "feedback": feedback,
                "source": result.source,
            },
            "mastery": updated.mastery if updated else node.mastery,
            "cleared": is_cleared(updated) if updated else False,
            "gate": gate_of(node.node_type),
        }

    async def _cards_result(
        self,
        ctx: ToolContext,
        service: LearningService,
        path: LearningPath,
        node: LearningNode,
        results: list[dict[str, Any]],
        *,
        added: int,
        kind: str,
    ) -> ToolResult:
        detail = await service.get_path(path.id)
        after = (await service.get_node(node.id)) or node
        state = compute_state(after, now=time.time())
        lines: list[str] = []
        if added:
            lines.append(f"（节点题不够，先补了 {added} 道）")
        for index, item in enumerate(results, start=1):
            lines.append(self._card_line(ctx, item, index=index, total=len(results)))
        gate = gate_of(after.node_type)
        lines.append(
            f"节点「{after.title}」现在的状态：掌握度 {after.mastery:.1f}"
            + (f"（门 {gate:.0f} 分）" if gate is not None else "（定性节点）")
            + f"，{'已过门' if is_cleared(after) else '未过门'}"
            + f"；下一目标：{detail.next_target.reason if detail else '—'}"
        )
        return ToolResult(
            ok=True,
            output="\n".join(line for line in lines if line),
            detail={
                "action": kind,
                "path_id": path.id,
                "node": after.model_dump(),
                "cards": results,
                "mastery": after.mastery,
                "gate": gate_of(after.node_type),
                "gate_kind": after.gate_kind,
                "cleared": is_cleared(after),
                "state": state,
                "stats": detail.stats.model_dump() if detail else None,
                "next": detail.next_target.model_dump() if detail else None,
            },
        )

    async def _assess(
        self, ctx: ToolContext, service: LearningService, path: LearningPath
    ) -> ToolResult:
        """定性门：让用户用自己的话讲一遍，LLM 判过不过（fail-closed）。"""
        prompts = get_prompt_manager()
        detail = await service.get_path(path.id)
        if detail is None:
            return ToolResult(ok=False, output=f"学习路径 {path.id} 不存在。")
        node, problem = await self._target_node(ctx, service, detail, kind="assess")
        if problem is not None:
            return problem
        assert node is not None
        ask_fn = self._ask_fn(ctx)
        if ask_fn is None:
            return ToolResult(ok=False, output="当前环境不支持回合中途提问（无前端连接）。")

        question = prompts.render("mastery", ctx.language, "assess.ask", title=node.title).strip()
        context = prompts.render(
            "mastery", ctx.language, "assess.card_context", title=node.title
        ).strip()
        interaction = await service.open_interaction(
            path_id=path.id,
            node_id=node.id,
            kind="assess",
            card_prompt=node.title,
            session_id=ctx.session_id or None,
            turn_id=ctx.turn_id,
        )
        answer = await ask_fn(
            question=question,
            options=[],
            ask_id=new_id("ask"),
            allow_free_text=True,
            context=context,
        )
        if not (answer or "").strip():
            return ToolResult(
                ok=True,
                output="用户这次没有讲（超时或跳过），下一轮我再问一次。",
                detail={
                    "action": "assess",
                    "path_id": path.id,
                    "node": node.model_dump(),
                    "cards": [
                        {
                            "question": {
                                "id": "",
                                "stem": question,
                                "type": "assess",
                                "options": [],
                            },
                            "answer": "",
                            "answered": False,
                        }
                    ],
                    "cleared": is_cleared(node),
                    "next": detail.next_target.model_dump(),
                },
            )
        tracker = self._tracker(ctx)
        try:
            assessment = await assessor_from_settings(language=ctx.language).assess(
                node_title=node.title,
                description=node.description,
                answer=answer,
                tracker=tracker,
            )
        except LLMError as exc:
            await service.close_interaction(
                interaction.id,
                user_answer=answer,
                correct=None,
                score=None,
                feedback=f"评定调用失败：{exc.message}",
                grade_source="assessor",
                status="abandoned",
                session_id=ctx.session_id or None,
                turn_id=ctx.turn_id,
            )
            return ToolResult(
                ok=False,
                output=f"定性评定这次没跑成（{exc.message}），先别下结论：跟用户说清可以再讲一遍，或稍后再试。",
            )
        updated = await service.record_qualitative(node.id, passed=assessment.passed)
        await service.close_interaction(
            interaction.id,
            user_answer=answer,
            correct=assessment.passed,
            score=1.0 if assessment.passed else 0.0,
            feedback=assessment.feedback,
            grade_source="assessor",
            session_id=ctx.session_id or None,
            turn_id=ctx.turn_id,
        )
        after = updated or node
        detail = await service.get_path(path.id)
        card = {
            "question": {"id": "", "stem": question, "type": "assess", "options": []},
            "interaction_id": interaction.id,
            "answer": answer,
            "answered": True,
            "grading": {
                "correct": assessment.passed,
                "score": 1.0 if assessment.passed else 0.0,
                "feedback": assessment.feedback,
                "source": "assessor",
            },
            "mastery": after.mastery,
            "cleared": assessment.passed,
            "gate": None,
        }
        verdict = "讲清楚了，这一节点算过关" if assessment.passed else "还差点意思：这一节点没过"
        return ToolResult(
            ok=True,
            output="\n".join(
                line
                for line in [
                    f"节点「{after.title}」的定性评定：{verdict}。",
                    (assessment.feedback or "").strip(),
                    f"下一目标：{detail.next_target.reason if detail else '—'}",
                ]
                if line
            ),
            detail={
                "action": "assess",
                "path_id": path.id,
                "node": after.model_dump(),
                "cards": [card],
                "mastery": after.mastery,
                "gate": None,
                "gate_kind": after.gate_kind,
                "cleared": assessment.passed,
                "state": after.state,
                "stats": detail.stats.model_dump() if detail else None,
                "next": detail.next_target.model_dump() if detail else None,
            },
        )

    async def _grade(
        self, ctx: ToolContext, service: LearningService, path: LearningPath
    ) -> ToolResult:
        """收尾未决题（卡超时/回合被停后唯一出口）。幂等：已判过的行直接回放结果。"""
        interaction_id = str(ctx.args.get("interaction_id") or "").strip()
        interaction = (
            await service.get_interaction(interaction_id)
            if interaction_id
            else await service.pending_interaction(path.id)
        )
        if interaction is None:
            return ToolResult(
                ok=False, output="现在没有未决的题：用 quiz 出新题，或 status 看下一目标。"
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
                    "cards": [self._replay_card(interaction)],
                    "replayed": True,
                },
            )
        answer = str(ctx.args.get("answer") or "").strip()
        if not answer:
            return ToolResult(
                ok=False,
                output=f"grade 需要 answer（这道题还在等作答：{_preview(interaction.card_prompt)}）",
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
                    output="没能读出用户选的是哪个选项：请让用户直接给选项标签（如 B）。",
                )
        result, feedback = await grade_with_fallback(
            question, submission, language=ctx.language, tracker=self._tracker(ctx)
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
        detail = await service.get_path(path.id)
        card = {
            "question": self._question_detail(question, reveal=question.answer),
            "interaction_id": interaction.id,
            "answer": submission,
            "answered": True,
            "grading": {
                "correct": result.correct,
                "score": result.score,
                "feedback": feedback,
                "source": result.source,
            },
            "mastery": updated.mastery if updated else 0.0,
            "cleared": is_cleared(updated) if updated else False,
            "gate": gate_of(updated.node_type) if updated else None,
        }
        return ToolResult(
            ok=True,
            output=self._card_line(ctx, card, index=1, total=1),
            detail={
                "action": "grade",
                "path_id": path.id,
                "node": updated.model_dump() if updated else None,
                "cards": [card],
                "mastery": updated.mastery if updated else 0.0,
                "gate": gate_of(updated.node_type) if updated else None,
                "gate_kind": updated.gate_kind if updated else None,
                "cleared": is_cleared(updated) if updated else False,
                "stats": detail.stats.model_dump() if detail else None,
                "next": detail.next_target.model_dump() if detail else None,
            },
        )

    # ---- 目标节点与题目 ----

    async def _target_node(
        self,
        ctx: ToolContext,
        service: LearningService,
        detail: PathDetail,
        *,
        kind: str,
    ) -> tuple[LearningNode | None, ToolResult | None]:
        """定目标节点（参数优先，其次服务端现算的下一目标），并校验动作适配。"""
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
            return None, ToolResult(
                ok=False, output=f"节点 {node_id or target.node_id} 不在本路径里。"
            )
        if kind == "probe":
            if gate_of(node.node_type) is None:
                return None, ToolResult(
                    ok=False,
                    output=f"「{node.title}」是定性节点，没有摸底这一步：用 assess 让它用自己的话讲一遍。",
                )
            attempted, interactions, assessed = await service.node_evidence(node.id)
            if attempted or interactions or assessed:
                return None, ToolResult(
                    ok=False,
                    output=(
                        f"「{node.title}」已经不是没碰过的节点了（作答 {attempted} 次、"
                        f"交互 {interactions} 次）——摸底只对全新的定量节点用，这里用 quiz。"
                    ),
                )
        if kind == "assess" and gate_of(node.node_type) is not None:
            return None, ToolResult(
                ok=False,
                output=f"「{node.title}」是定量节点（门 {gate_of(node.node_type):.0f} 分），过门看做题：用 quiz。",
            )
        return node, None

    async def _next_question(
        self, ctx: ToolContext, node: LearningNode, asked: list[str]
    ) -> Question | None:
        wanted = str(ctx.args.get("question_id") or "").strip()
        if wanted:
            question = await get_question_bank().get_question(wanted)
            if question is None or question.node_id != node.id:
                return None
            return question
        return await pick_next_question(node.id, exclude=asked)

    async def _ask_answer(
        self,
        ask_fn: AskFn,
        *,
        question: str,
        options: list[dict[str, str]],
        context: str,
    ) -> str:
        return await ask_fn(
            question=question,
            options=options,
            ask_id=new_id("ask"),
            allow_free_text=True,
            context=context,
        )

    # ---- 组装与文案 ----

    @staticmethod
    def _ask_fn(ctx: ToolContext) -> AskFn | None:
        return ctx.metadata.get("ask_user_fn")

    @staticmethod
    def _tracker(ctx: ToolContext) -> CostTracker | None:
        return ctx.metadata.get("cost_tracker")

    @staticmethod
    def _card_count(ctx: ToolContext, *, default: int) -> int:
        try:
            value = int(ctx.args.get("cards", default))
        except (TypeError, ValueError):
            return default
        return max(1, min(value, MAX_CARDS))

    @staticmethod
    def _question_detail(question: Question, *, reveal: str | None = None) -> dict[str, Any]:
        """题面 + 选项；`reveal` 只在该题**判分之后**传（答案键不外泄，见模块头 ④）。"""
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

    @staticmethod
    def _replay_card(interaction: LearningInteraction) -> dict[str, Any]:
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

    @staticmethod
    def _card_line(ctx: ToolContext, card: dict[str, Any], *, index: int, total: int) -> str:
        question = card.get("question") or {}
        if not card.get("answered"):
            return f"第 {index}/{total} 题：{_preview(str(question.get('stem') or ''))}\n{card.get('note') or ''}"
        grading = card.get("grading") or {}
        parts = [
            f"第 {index}/{total} 题：{_preview(str(question.get('stem') or ''))}",
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

    @staticmethod
    def _action_label(prompts: Any, language: str, action: str | None) -> str:
        if not action:
            return "—"
        return prompts.render("mastery", language, f"state_block.action_{action}").strip() or action

    @staticmethod
    def _tree_lines(nodes: list[LearningNode]) -> str:
        lines = []
        for node in nodes:
            gate = gate_of(node.node_type)
            gate_text = f"门 {gate:.0f} 分" if gate is not None else "定性门"
            lines.append(f"{'  ' * node.depth}- {node.title}（{gate_text}）")
        return "\n".join(lines)

    def _status_lines(self, ctx: ToolContext, detail: PathDetail | None) -> str:
        """人读文本：路径地图 + 下一目标 + 薄弱点 + 复习建议（细节都在 detail 里）。"""
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
                action=self._action_label(prompts, ctx.language, target.action),
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
                lines.append(
                    f"- {weak.title}：{gap}（作答 {weak.attempted} 题，错过 {weak.wrong} 次）"
                )
        if detail.reviews:
            lines.append("复习安排：")
            for review in detail.reviews[:5]:
                when = "已到期" if review.overdue else "未到期"
                lines.append(f"- {review.title}：{when}（第 {review.review_stage + 1} 档）")
        return "\n".join(lines)

    async def _resolve(self, ctx: ToolContext, service: LearningService) -> LearningPath | None:
        """工具参数给的 path_id 优先，其次本回合 config（能力注入），最后按会话反查。"""
        path_id = str(ctx.args.get("path_id") or ctx.config.get("path_id") or "").strip()
        if path_id:
            return await service.get_path_model(path_id)
        return await service.get_path_by_session(ctx.session_id)

    @staticmethod
    async def _no_path(ctx: ToolContext, service: LearningService) -> ToolResult:
        """没有可用路径：把库里已有的列出来（模型看不到路径表，只能靠这里给）。"""
        given = str(ctx.args.get("path_id") or ctx.config.get("path_id") or "").strip()
        lines: list[str] = []
        if given:
            lines.append(f"学习路径 {given} 不存在。")
        elif ctx.session_id:
            lines.append("这个会话还没绑学习路径。")
        else:
            lines.append("这个会话没有可用的学习路径（没有会话上下文）。")
        summaries = await service.list_paths()
        if summaries:
            lines.append("库里已有的路径（对得上就 action=switch path_id=...）：")
            for summary in summaries[:10]:
                lines.append(
                    f"- 《{summary.path.title}》：已过门 {summary.stats.mastered}/"
                    f"{summary.stats.total}，下一目标 {summary.next_title or '—'}；id={summary.path.id}"
                )
        lines.append("都不合适就 action=build 新建一条（给 topic 与 nodes）。")
        return ToolResult(ok=False, output="\n".join(lines))


def _preview(text: str, limit: int = STEM_PREVIEW_CHARS) -> str:
    plain = " ".join(str(text).split())
    return plain if len(plain) <= limit else plain[:limit] + "…"
