"""mastery_path 能力（§7.5 重做版）：**门就是游标** + 服务端状态块 + 自由循环。

对外只有一个阶段 `responding`（§6.4 明令：阶段是能力的对外契约，内部三步不分段）：

1. **解析路径**（零 LLM）：`config.path_id` → 本会话绑定的路径；一条都没有就是「还没建树」，
   提示词换成 `system_new`（先 `paths` 看已有的 → 有对得上的 `switch` → 没有才 `build`）；
2. **状态块**（零 LLM，纯服务端）：路径地图 / 全部路径 / **下一目标**（服务端按「哪些节点已过门」
   现算，没有 `current_node_id` 这一列）/ 未决的卡 / 到期复习 / 各节点题目数 / 薄弱点 /
   **已作答过的错题**（题干 + 正确答案 + 解析——这就是「答错后针对性重讲」的原料）。
   状态块进 system 提示词，**不当作正文流给用户**（上一版把简报当正文发，用户得先读一屏机器生成的
   清单，这次回归「正文只有模型讲的话」）；
3. **自由循环**：`run_agent_loop` 里模型讲解、调 mastery 工具（`quiz`/`probe`/`assess` 会当场
   弹卡片给用户作答，答复与判分都在同一个工具调用里闭环）。

**补题搬进工具了**：上一版在能力里「启动时确定性补题」，现在只在真的要用题时（`quiz`）才补，
所以本能力这一版**零 LLM 调用**（除了循环本身），每回合步数更可预测。

两处与 deep_question 不同的地方（照旧）：
- 正文**不吞**：讲解逐字流出去（真 bus 直接转），收尾的 `content_done` 用 `_TurnTranscriptBus`
  记下的全文——见该类的说明；
- 工具集刻意排除 brainstorm / reason / consult_subagent（这三个自己会发 LLM 调用，会吃脚本步数）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nnnu.capabilities._shared import (
    MountedTools,
    build_user_message,
    mount_tools,
    session_history,
)
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest, Stage
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.models import PathDetail
from nnnu.services.learning.policy import gate_of, strategy_key
from nnnu.services.learning.service import LearningService, get_learning_service
from nnnu.services.llm.factory import ModelConfig, create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.question_bank.service import get_question_bank

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

# 配置项（键名不能动：tests/test_plugins_api.py 断言了 path_id / practice_count）
PRACTICE_COUNT_MIN = 1
PRACTICE_COUNT_MAX = 5
PRACTICE_COUNT_DEFAULT = 3

# 状态块的自定尺寸（§7.5）：够模型看清全局，又不至于把上下文挤爆
STATE_BLOCK_NODES = 12
STATE_BLOCK_QUESTIONS = 8
STATE_BLOCK_PATHS = 10
STATE_BLOCK_WRONG = 3
STATE_BLOCK_WEAK = 5
STEM_PREVIEW_CHARS = 120

# 本阶段挂哪些工具：mastery（讲解/出题/摸底/评定）+ 查资料 + 澄清 + 执行。
# brainstorm / reason / consult_subagent 排除在外（它们自己会发 LLM 调用）。
STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "responding": (
        "mastery",
        "ask_user",
        "rag",
        "attachment_search",
        "web_search",
        "code_execution",
    ),
}


class _TurnTranscriptBus:
    """转发一切、把正文按序记下来的总线（本能力的循环专用）。

    循环只在「这一轮没调工具」时才把正文并进 `LoopOutcome.final_text`：讲解那一轮
    （带 `quiz`/`assess` 调用）的文字只以 `content_delta` 流出去，不进 final_text。
    收尾若只发 final_text，前端拿 content_done 替换气泡内容（`useChat.ts`），
    刚讲完的那段会当场消失。所以把流出去的正文记下来，收尾时发**记录全文**，
    保证「看到的 == 存下的」（§7.3）。

    顺手把循环内部那次 `content_done` 吞掉：本能力只在收尾发一次终局正文，
    少了它中间那次替换就不会把气泡先刷成半截正文。

    不继承 StreamBus（照 question 能力的 `_SilentContentBus`）：继承会多出一份
    `terminal_emitted` / `_history` 状态，收尾判断就分叉了。
    """

    def __init__(self, bus: StreamBus) -> None:
        self._bus = bus
        self.text = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bus, name)

    async def emit_content_delta(self, *, text: str) -> Any:
        self.text += text
        return await self._bus.emit_content_delta(text=text)

    async def emit_content_done(self, *, full_text: str) -> None:
        return None  # 由能力收尾统一发（记录全文）


class MasteryCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="mastery_path",
        version="1.1.0",
        stages=[
            Stage(
                key="responding",
                label_i18n="stages.responding.label",
                max_rounds=12,
                max_tokens=4096,
            )
        ],
        config_schema={
            "path_id": {
                "type": "string",
                "default": "",
                "description": "学习路径 id；留空则用当前会话绑定的路径",
            },
            "practice_count": {
                "type": "integer",
                "minimum": PRACTICE_COUNT_MIN,
                "maximum": PRACTICE_COUNT_MAX,
                "default": PRACTICE_COUNT_DEFAULT,
                "description": "quiz 补题时一个节点补几道（1–5）",
            },
        },
        default_model_role="chat",
    )

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        prompts = get_prompt_manager()
        lang = ctx.language
        spec, errors = self._read_config(ctx)
        if errors:
            # §9.2：非法组合报 error(recoverable=false)，不建客户端、不打模型
            await bus.emit_error(message="；".join(errors), recoverable=False)
            return
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            await bus.emit_error(message=str(exc), recoverable=False)
            return
        path = (
            await service.get_path_model(spec["path_id"])
            if spec["path_id"]
            else await service.get_path_by_session(ctx.session.id)
        )
        if path is None and spec["path_id"]:
            # 只有「指名道姓给的 id 找不到」才是错（零 LLM 打回）；一条路径都没绑 = 第一回合，
            # 这一回合的活就是建树（或切到已有路径）——这就是「路径由聊天驱动生成」
            await bus.emit_error(
                message=(
                    f"找不到学习路径 {spec['path_id']}：从学习看板进入已建的路径，"
                    "或让模型用 mastery 的 paths / build 重建一条。"
                ),
                recoverable=False,
            )
            return
        if path is not None and path.session_id != ctx.session.id:
            # 聊天驱动生成的路径第一回合就把会话绑上，后几轮才找得到它
            await service.bind_session(path.id, ctx.session.id)

        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        await bus.emit_status(
            stage="responding",
            message=prompts.render("mastery", lang, "stages.responding.label") or "responding",
        )

        detail: PathDetail | None = None
        if path is not None:
            detail = await service.get_path(path.id)  # 内部重算四态 + 现算下一目标
            if detail is None:
                await bus.emit_error(message=f"学习路径 {path.id} 已被删除", recoverable=False)
                return
        state_block = await self._state_block(ctx, service, lang, detail)

        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        client = create_client(
            mc.model, provider_id=mc.provider_id, base_url=mc.base_url, api_key=mc.api_key
        )
        common = {
            "language": {"zh": "中文", "en": "English"}.get(lang, lang),
            "tools": mounted.tool_lines(),
            "rules": prompts.render("mastery", lang, "rules").strip(),
            "state_block": state_block,
            "kb_note": (
                prompts.render("mastery", lang, "kb_note", kbs="、".join(mounted.kb_names))
                if mounted.kb_names
                else ""
            ),
        }
        if detail is None:
            system = prompts.render("mastery", lang, "system_new", **common)
        else:
            target = detail.next_target
            node = detail.node(target.node_id)
            gate = gate_of(node.node_type) if node else None
            system = prompts.render(
                "mastery",
                lang,
                "system",
                node_title=node.title if node else detail.path.title,
                node_type_label=self._node_type_label(prompts, lang, node),
                gate_text=(
                    prompts.render("mastery", lang, "gate_text.quantitative", gate=f"{gate:.0f}")
                    if gate is not None
                    else prompts.render("mastery", lang, "gate_text.qualitative")
                ),
                strategy=self._strategy(prompts, lang, node),
                **common,
            )
        history = [
            {"role": "system", "content": system},
            *session_history(ctx),
            await build_user_message(ctx, bus, mc.provider_id, mc.model),
        ]
        relay = _TurnTranscriptBus(bus)
        outcome = await run_agent_loop(
            ctx,
            relay,  # type: ignore[arg-type]  # 只多记正文 + 吞掉循环内部那次 content_done
            self._deps(
                ctx,
                self.manifest.stages[0],
                mounted.filtered(STAGE_TOOLS["responding"]),
                client,
                mc,
            ),
            history,
        )
        if not outcome.completed:
            logger.warning("mastery_path 循环未正常完成，按已完成的部分收尾")
        if outcome.completed:
            await bus.emit_content_done(full_text=relay.text)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if outcome.completed and not bus.terminal_emitted:
            # 工具轨迹要落库：出卡/判分就是这一回合的对话本身，刷新后结果卡不能掉成 JSON
            await bus.emit_done(response=relay.text, tool_calls=outcome.tool_calls)

    # ---- 配置 ----

    @staticmethod
    def _read_config(ctx: UnifiedContext) -> tuple[dict[str, Any], list[str]]:
        raw = ctx.config
        errors: list[str] = []
        count = PRACTICE_COUNT_DEFAULT
        try:
            count = int(raw.get("practice_count", PRACTICE_COUNT_DEFAULT))
        except (TypeError, ValueError):
            errors.append(f"补题数量 {raw.get('practice_count')!r} 不是整数")
        else:
            if not PRACTICE_COUNT_MIN <= count <= PRACTICE_COUNT_MAX:
                errors.append(
                    f"补题数量 {count} 超出范围（{PRACTICE_COUNT_MIN}–{PRACTICE_COUNT_MAX}）"
                )
        return {
            "path_id": str(raw.get("path_id") or "").strip(),
            "practice_count": count,
        }, errors

    # ---- 状态块（纯服务端，进 system 提示词） ----

    async def _state_block(
        self,
        ctx: UnifiedContext,
        service: LearningService,
        lang: str,
        detail: PathDetail | None,
    ) -> str:
        """服务端现算的状态块：模型看不到库，这里给它全局（**只含已作答题的答案键**）。"""
        prompts = get_prompt_manager()

        def render(key: str, **vars: Any) -> str:
            return prompts.render("mastery", lang, f"state_block.{key}", **vars).strip()

        lines = [render("heading")]
        if detail is None:
            lines.append(render("no_path"))
            summaries = await service.list_paths()
            if summaries:
                lines.append(render("paths_intro"))
                for summary in summaries[:STATE_BLOCK_PATHS]:
                    lines.append(
                        render(
                            "path_line",
                            title=summary.path.title,
                            mastered=summary.stats.mastered,
                            total=summary.stats.total,
                            due=summary.stats.due,
                            next=summary.next_title or "—",
                            action=self._action_label(prompts, lang, summary.next_action),
                            id=summary.path.id,
                        )
                    )
            lines.append(render("paths_hint"))
            return "\n".join(line for line in lines if line)

        path, stats, target = detail.path, detail.stats, detail.next_target
        lines.append(
            render(
                "path",
                title=path.title,
                total=stats.total,
                mastered=stats.mastered,
                due=stats.due,
                progress=f"{stats.progress:.0%}",
                id=path.id,
            )
        )
        lines.append(
            render(
                "next",
                reason=target.reason or "（没有目标）",
                action=self._action_label(prompts, lang, target.action),
            )
        )
        pending = await service.pending_interaction(path.id)
        if pending is not None:
            lines.append(render("pending", stem=_preview(pending.card_prompt)))
        counts = await service.question_counts()
        lines.append(render("nodes_intro"))
        for node in detail.nodes[:STATE_BLOCK_NODES]:
            lines.append(
                render(
                    "node_line",
                    title=node.title,
                    type_label=self._node_type_label(prompts, lang, node),
                    state_label=prompts.render("mastery", lang, f"state_labels.{node.state}")
                    or node.state,
                    mastery=f"{node.mastery:.1f}",
                    gate_text=self._gate_text(prompts, lang, node),
                )
            )
        if detail.reviews:
            lines.append(render("reviews_intro"))
            for review in detail.reviews[:STATE_BLOCK_QUESTIONS]:
                lines.append(
                    render(
                        "review_line",
                        title=review.title,
                        type_label=self._node_type_label(
                            prompts, lang, node=None, node_type=review.node_type
                        ),
                        stage=review.review_stage + 1,
                    )
                )
        question_lines: list[str] = []
        for node in detail.nodes[:STATE_BLOCK_QUESTIONS]:
            total, attempted, wrong = counts.get(node.id, (0, 0, 0))
            question_lines.append(
                render(
                    "question_line" if total else "no_questions",
                    title=node.title,
                    total=total,
                    attempted=attempted,
                    wrong=wrong,
                )
            )
        if question_lines:
            lines.append(render("questions_intro"))
            lines.extend(question_lines)
        if detail.weak_points:
            lines.append(render("weak_intro"))
            for weak in detail.weak_points[:STATE_BLOCK_WEAK]:
                lines.append(
                    render(
                        "weak_line",
                        title=weak.title,
                        mastery=f"{weak.mastery:.1f}",
                        gate=f"{weak.gate:.0f}" if weak.gate else "—",
                        attempted=weak.attempted,
                        wrong=weak.wrong,
                    )
                )
        lines.extend(await self._wrong_material(ctx, lang, target.node_id))
        return "\n".join(line for line in lines if line)

    async def _wrong_material(
        self, ctx: UnifiedContext, lang: str, node_id: str | None
    ) -> list[str]:
        """已有作答记录的错题（题干 + 答案 + 解析）——**唯一的答案键入口**。

        只取 `filter="wrong"`（即已经作答过的题）；未作答的新题永不进提示词，
        否则「考考我」就变成了「把答案念给我听」（答案不外泄约束 ②）。
        """
        if not node_id:
            return []
        prompts = get_prompt_manager()
        try:
            wrong = await get_question_bank().list_questions(
                node_id=node_id, filter="wrong", limit=STATE_BLOCK_WRONG
            )
        except Exception:  # 题库读失败不该挡住讲解
            logger.exception("读错题材料失败，本节按无错题处理")
            return []
        if not wrong:
            return []
        lines = [prompts.render("mastery", lang, "state_block.wrong_intro").strip()]
        for question in wrong:
            lines.append(
                prompts.render(
                    "mastery",
                    lang,
                    "state_block.wrong_line",
                    stem=question.stem,
                    answer=question.answer,
                    explanation=question.explanation or "（没写解析）",
                ).rstrip()
            )
        lines.append(prompts.render("mastery", lang, "remedial", count=len(wrong)).strip())
        return [line for line in lines if line]

    # ---- 提示词小工具 ----

    @staticmethod
    def _action_label(prompts: Any, lang: str, action: str | None) -> str:
        if not action:
            return "—"
        return prompts.render("mastery", lang, f"state_block.action_{action}").strip() or action

    @staticmethod
    def _strategy(prompts: Any, lang: str, node: Any) -> str:
        key = strategy_key(node.node_type) if node is not None else "concept"
        return prompts.render("mastery", lang, f"strategies.{key}").strip()

    @staticmethod
    def _gate_text(prompts: Any, lang: str, node: Any) -> str:
        gate = gate_of(node.node_type)
        if gate is None:
            return prompts.render("mastery", lang, "state_block.gate_qualitative").strip()
        return prompts.render(
            "mastery", lang, "state_block.gate_quantitative", gate=f"{gate:.0f}"
        ).strip()

    @staticmethod
    def _node_type_label(prompts: Any, lang: str, node: Any = None, *, node_type: str = "") -> str:
        kind = node_type or (node.node_type if node is not None else "")
        if not kind:
            return ""
        return prompts.render("mastery", lang, f"node_types.{kind}") or kind

    @staticmethod
    def _deps(
        ctx: UnifiedContext,
        stage: Stage,
        mounted: MountedTools,
        client: LLMClient,
        mc: ModelConfig,
    ) -> LoopDeps:
        """回合内一份 LoopDeps（预算会被循环就地扣减，不能跨回合复用）。"""
        config = ctx.config
        return LoopDeps(
            client=client,
            tools=mounted.tool_set(),
            model=mc.model,
            provider=mc.provider_id or "",
            max_rounds=stage.max_rounds,
            max_output_tokens=stage.max_tokens,
            token_budget=config.get("token_budget", 32000),
            temperature=config.get("temperature", mc.temperature),
            reasoning_effort=config.get("reasoning_effort", mc.reasoning_effort),
            thinking_extra=build_reasoning_kwargs(
                provider_id=mc.provider_id or "",
                model=mc.model,
                reasoning_effort=config.get("reasoning_effort"),
            ),
        )


def _preview(text: str, limit: int = STEM_PREVIEW_CHARS) -> str:
    plain = " ".join(str(text or "").split())
    return plain if len(plain) <= limit else plain[:limit] + "…"
