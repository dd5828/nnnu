"""visualize 能力（§7.7）：analyzing → generating → reviewing 三阶段。

设计要点（P7 决策，见 STAGE_LOG 偏离清单）：
- 三阶段各跑一次 LLM 调用，但**阶段原文不进正文**（`SilentBus` 吞掉）：简报是 JSON、
  生成段是代码块、复查段是 JSON，都只给能力自己解析。正文是能力按契约自己拼的
  「标题 + 一句说明 + 恰好一个渲染围栏」，落库=界面所见（重载后仍在的唯一通道）；
- **确定性校验器是唯一验收闸门**：生成段产出先过 `validate_visualization`，不过就让
  模型修一次；再不过——echarts 按 §7.7「ECharts 报错降级 SVG」强制改 SVG 重画一次，
  其余类型用兜底 HTML 收场（正文注明降级）；
- 复查段按 §7.7 保留 LLM 自审，但它只能提交「修好的完整代码」，且这份代码必须再过
  校验器才会被采纳——模型说 ok（或改得不过关）都按上一版渲染；
- render_type 由分析段定；用户在下拉里 pin 了类型就强制改写；分析段 JSON 不可用时
  走 `guess_render_type` 的关键词兜底；
- manim_video 支线：分析段判出动画就转 `services/render/animator.py` 的六阶段流水线
  （math_animator 能力同款，共用同一份提示词与假渲染器）；依赖不可用走友好报错。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nnnu.capabilities._shared import (
    MountedTools,
    SilentBus,
    append_user_text,
    build_user_message,
    complete_with_cost,
    mount_tools,
    session_history,
)
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest, Stage
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import ModelConfig, create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.render.animator import AnimationRequest, run_animation
from nnnu.services.render.models import MANIM_RENDER_TYPE, VISUAL_RENDER_TYPES
from nnnu.services.render.service import DependencyReport, get_render_service
from nnnu.services.render.validation import (
    build_fallback_html,
    extract_render_code,
    guess_render_type,
    parse_analysis,
    validate_visualization,
)

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

# 用户可 pin 的取值：auto 交给分析段路由，其余强制（manim_video 不可 pin）
RENDER_MODES: tuple[str, ...] = ("auto", *VISUAL_RENDER_TYPES)

# 每阶段挂哪些工具（与设置里开关求交）：只有分析段需要翻资料
STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "analyzing": ("rag", "attachment_search", "web_search", "web_fetch"),
    "generating": (),
    "reviewing": (),
}

# 渲染类型 → 正文围栏语言（与 validation.FENCE_ALIASES 的第一别名一致）
FENCE_LANG: dict[str, str] = {
    "svg": "svg",
    "echarts": "echarts",
    "mermaid": "mermaid",
    "html": "html",
}


def _manim_available() -> bool:
    """Manim 依赖体检；渲染服务未装配（如裸跑能力）一律当不可用。"""
    try:
        return get_render_service().available().ok
    except RuntimeError:
        return False


class VisualizeCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="visualize",
        version="1.0.0",
        stages=[
            Stage(
                key="analyzing", label_i18n="stages.analyzing.label", max_rounds=4, max_tokens=1024
            ),
            Stage(
                key="generating",
                label_i18n="stages.generating.label",
                max_rounds=4,
                max_tokens=4096,
            ),
            Stage(
                key="reviewing", label_i18n="stages.reviewing.label", max_rounds=2, max_tokens=4096
            ),
        ],
        config_schema={
            "render_type": {
                "type": "string",
                "enum": list(RENDER_MODES),
                "default": "auto",
                "description": "auto = 按需求自动选；其余为固定渲染类型",
            },
            "style_hint": {"type": "string", "description": "附加风格说明（可选）"},
        },
        default_model_role="chat",
    )

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        prompts = get_prompt_manager()
        lang = ctx.language
        pinned = str(ctx.config.get("render_type") or "auto")
        if pinned not in RENDER_MODES:
            # §9.2：非法组合报 error(recoverable=false)，不去猜用户想要哪种
            await bus.emit_error(
                message=f"非法渲染类型 {pinned!r}：支持 {'、'.join(RENDER_MODES)}",
                recoverable=False,
            )
            return
        style_hint = str(ctx.config.get("style_hint") or "").strip()

        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        common: dict[str, Any] = {
            "language": {"zh": "中文", "en": "English"}.get(lang, lang),
            "tools": mounted.tool_lines(),
            "kb_note": (
                prompts.render("visualize", lang, "kb_note", kbs=", ".join(mounted.kb_names))
                if mounted.kb_names
                else ""
            ),
        }
        preamble = prompts.render("visualize", lang, "system", **common)

        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        provider_id, model = mc.provider_id, mc.model
        client = create_client(
            model, provider_id=provider_id, base_url=mc.base_url, api_key=mc.api_key
        )
        base_history = session_history(ctx)
        question = await build_user_message(ctx, bus, provider_id, model)

        completed = True
        body = ""
        title = prompts.render("visualize", lang, "body.untitled")
        description = ""
        render_type = "svg"
        code = ""
        degraded = False

        # ---- 1. 分析 ----
        catalog = prompts.render("visualize", lang, "render_types.catalog")
        if not _manim_available():
            catalog = (
                f"{catalog}\n{prompts.render('visualize', lang, 'render_types.manim_unavailable')}"
            )
        pin_note = (
            prompts.render("visualize", lang, "stages.analyzing.pin_note", render_type=pinned)
            if pinned != "auto"
            else ""
        )
        brief_raw = await self._loop_stage(
            ctx,
            bus,
            stage=self._stage("analyzing"),
            label=prompts.render("visualize", lang, "stages.analyzing.label"),
            preamble=preamble,
            system=prompts.render(
                "visualize", lang, "stages.analyzing.system", render_types=catalog
            ),
            task=prompts.render("visualize", lang, "stages.analyzing.task", pin_note=pin_note),
            client=client,
            mc=mc,
            mounted=mounted,
            base_history=base_history,
            question=question,
        )
        brief_text = brief_raw or ""
        if brief_raw is None:
            completed = False
        else:
            brief = parse_analysis(brief_text)
            render_type = str(brief.get("render_type") or "").strip().lower()
            if render_type not in (*VISUAL_RENDER_TYPES, MANIM_RENDER_TYPE):
                render_type = guess_render_type(ctx.message.content)
            if pinned != "auto":
                render_type = pinned  # 用户 pin 的优先，覆盖模型判断
            title = str(brief.get("title") or "").strip() or title
            description = str(brief.get("description") or "").strip()
            await bus.emit_status(
                stage="analyzing",
                message=prompts.render(
                    "visualize",
                    lang,
                    "render_types.chosen",
                    render_type=render_type,
                    description=description,
                ),
            )

        # ---- 1b. 数学动画支线：交给六阶段流水线（正文与 done 由下面的统一收尾发） ----
        if completed and render_type == MANIM_RENDER_TYPE:
            body = await self._animation_branch(
                ctx,
                bus,
                client=client,
                mc=mc,
                mounted=mounted,
                base_history=base_history,
                question=question,
                style_hint=style_hint,
            )
            completed = False  # 生成/复查是文本渲染类型的路径，动画不进

        # ---- 2. 生成（含校验/修复/降级） ----
        if completed:
            fence_lang = FENCE_LANG[render_type]
            gen_system = self._generating_system(prompts, lang, render_type, common)
            gen_task = prompts.render(
                "visualize",
                lang,
                "stages.generating.task",
                render_type=render_type,
                brief=brief_text.strip(),
                fence_lang=fence_lang,
                style_hint=(
                    prompts.render(
                        "visualize", lang, "stages.generating.style_hint", hint=style_hint
                    )
                    if style_hint
                    else ""
                ),
            )
            gen_text = await self._loop_stage(
                ctx,
                bus,
                stage=self._stage("generating"),
                label=prompts.render("visualize", lang, "stages.generating.label"),
                preamble=preamble,
                system=gen_system,
                task=gen_task,
                client=client,
                mc=mc,
                mounted=mounted,
                base_history=base_history,
                question=question,
            )
            if gen_text is None:
                completed = False
            else:
                code = extract_render_code(gen_text, render_type) or ""
                ok, reason = validate_visualization(code, render_type)
                if not ok:
                    logger.info("visualize 生成未过校验（%s）：%s，修一次", render_type, reason)
                    code = await self._repair(
                        ctx, client, mc, prompts, lang, render_type, fence_lang, code, reason
                    )
                    ok, reason = validate_visualization(code, render_type)
                if not ok:
                    # 两次不过：echarts 按 §7.7 降级 SVG 重画一次，其余垫兜底 HTML
                    render_type, code, fence_lang, degraded = await self._degrade(
                        ctx, client, mc, prompts, lang, render_type, title, brief_text, reason
                    )

        # ---- 3. 复查（LLM 自审；兜底页不复查） ----
        if completed and not degraded:
            await bus.emit_status(
                stage="reviewing",
                message=prompts.render("visualize", lang, "stages.reviewing.label"),
            )
            review_text = await self._plain_call(
                ctx,
                client,
                mc,
                system=prompts.render("visualize", lang, "stages.reviewing.system", **common),
                task=prompts.render(
                    "visualize",
                    lang,
                    "stages.reviewing.task",
                    render_type=render_type,
                    fence_lang=fence_lang,
                    code=code,
                    brief=brief_text.strip(),
                ),
            )
            code = self._apply_review(review_text, code, render_type)

        # ---- 收尾：拼正文（落库=界面所见）→ cost_summary → done ----
        # 动画支线的正文已在 _animation_branch 里发过 content_done（completed=False），
        # 这里只按 body 有无发 done——两条路径共用同一套终局防双发。
        if completed:
            body = self._assemble_body(
                prompts, lang, title, description, render_type, code, degraded
            )
            await bus.emit_content_done(full_text=body)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if body and not bus.terminal_emitted:
            await bus.emit_done(response=body)

    # ---- 阶段执行 ----

    async def _loop_stage(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        *,
        stage: Stage,
        label: str,
        preamble: str,
        system: str,
        task: str,
        client: LLMClient,
        mc: ModelConfig,
        mounted: MountedTools,
        base_history: list[dict[str, Any]],
        question: dict[str, Any],
    ) -> str | None:
        """跑一个带工具的阶段循环（正文被 SilentBus 吞掉）；未正常完成返回 None。"""
        prompts = get_prompt_manager()
        await bus.emit_status(stage=stage.key, message=label)
        history = [
            {"role": "system", "content": f"{preamble}\n\n{system}"},
            *base_history,
            append_user_text(
                question,
                prompts.render(
                    "visualize",
                    ctx.language,
                    "stage_input.first",
                    stage_task=task,
                ),
            ),
        ]
        outcome = await run_agent_loop(
            ctx,
            SilentBus(bus),  # type: ignore[arg-type]
            self._stage_deps(ctx, stage, mounted.filtered(STAGE_TOOLS[stage.key]), client, mc),
            history,
        )
        if not outcome.completed:
            logger.warning("visualize 阶段 %s 未正常完成，就此收尾", stage.key)
            return None
        return outcome.final_text or ""

    async def _plain_call(
        self,
        ctx: UnifiedContext,
        client: LLMClient,
        mc: ModelConfig,
        *,
        system: str,
        task: str,
        max_tokens: int = 4096,
    ) -> str:
        """阶段内部的非流式调用（修复、复查）：不挂工具，用量并入本回合成本。"""
        return await complete_with_cost(
            ctx,
            client,
            mc,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": task},
            ],
            temperature=mc.temperature,
            max_tokens=max_tokens,
        )

    # ---- 校验、修复、降级、复查 ----

    def _generating_system(self, prompts: Any, lang: str, render_type: str, common: dict) -> str:
        rules = "\n".join(
            filter(
                None,
                (
                    prompts.render("visualize", lang, f"rules.{render_type}", **common),
                    prompts.render("visualize", lang, "rules.common", **common),
                ),
            )
        )
        return prompts.render("visualize", lang, "stages.generating.system", rules=rules, **common)

    async def _repair(
        self,
        ctx: UnifiedContext,
        client: LLMClient,
        mc: ModelConfig,
        prompts: Any,
        lang: str,
        render_type: str,
        fence_lang: str,
        code: str,
        reason: str,
    ) -> str:
        """一次最小修复；修复产出抠不出来就保留原稿。"""
        text = await self._plain_call(
            ctx,
            client,
            mc,
            system=prompts.render("visualize", lang, "stages.generating.repair_system"),
            task=prompts.render(
                "visualize",
                lang,
                "stages.generating.repair_task",
                error=reason,
                fence_lang=fence_lang,
                code=code,
            ),
        )
        repaired = extract_render_code(text, render_type)
        return repaired if repaired else code

    async def _degrade(
        self,
        ctx: UnifiedContext,
        client: LLMClient,
        mc: ModelConfig,
        prompts: Any,
        lang: str,
        render_type: str,
        title: str,
        brief_text: str,
        reason: str,
    ) -> tuple[str, str, str, bool]:
        """两次校验不过的收场：echarts → 强制 SVG 重画一次；其余 → 兜底 HTML。"""
        if render_type == "echarts":
            logger.info("echarts 两次未过校验，按 §7.7 降级 SVG 重画")
            text = await self._plain_call(
                ctx,
                client,
                mc,
                system=self._generating_system(prompts, lang, "svg", {}),
                task=prompts.render(
                    "visualize",
                    lang,
                    "stages.generating.fallback_task",
                    failed_type="echarts",
                    error=reason,
                ),
            )
            candidate = extract_render_code(text, "svg") or ""
            ok, fallback_reason = validate_visualization(candidate, "svg")
            if ok:
                return "svg", candidate, "svg", False
            reason = fallback_reason
        note = prompts.render("visualize", lang, "body.degraded_note").lstrip("> ").strip()
        return "html", build_fallback_html(title, note, lang), "html", True

    def _apply_review(self, review_text: str, code: str, render_type: str) -> str:
        """复查结论：只有「改过的完整代码」且过校验器才替换。"""
        verdict = parse_analysis(review_text)
        if str(verdict.get("verdict") or "").strip().lower() != "fixed":
            return code
        candidate = str(verdict.get("code") or "")
        candidate = extract_render_code(candidate, render_type) or candidate.strip()
        if not candidate:
            return code
        ok, reason = validate_visualization(candidate, render_type)
        if not ok:
            logger.info("复查提交的修订未过校验（%s），保留原版", reason)
            return code
        return candidate

    def _assemble_body(
        self,
        prompts: Any,
        lang: str,
        title: str,
        description: str,
        render_type: str,
        code: str,
        degraded: bool,
    ) -> str:
        parts = [prompts.render("visualize", lang, "body.heading", title=title)]
        if description:
            parts.append(description)
        parts.append(
            prompts.render(
                "visualize",
                lang,
                "body.fence",
                fence=FENCE_LANG.get(render_type, render_type),
                code=code,
            )
        )
        if degraded:
            parts.append(prompts.render("visualize", lang, "body.degraded_note"))
        return "\n\n".join(parts)

    # ---- 数学动画支线 ----

    async def _animation_branch(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        *,
        client: LLMClient,
        mc: ModelConfig,
        mounted: MountedTools,
        base_history: list[dict[str, Any]],
        question: dict[str, Any],
        style_hint: str,
    ) -> str:
        """manim_video 支线：依赖不可用就友好报错；可用则跑六阶段流水线。

        提示词与装配件与 math_animator 能力完全共用（services/render/animator.py）：
        这里只是「分析段判出了动画」这个进场方式不同。正文在这里就发 content_done，
        统一收尾只按 body 有无补 done。
        """
        prompts = get_prompt_manager()
        lang = ctx.language
        try:
            service = get_render_service()
            report = service.available()
        except RuntimeError:
            logger.warning("渲染服务未装配，数学动画支线按依赖缺失处理")
            service = None
            report = DependencyReport(missing=["manim", "ffmpeg"])
        if service is None or not report.ok:
            await bus.emit_error(
                message=prompts.render(
                    "math_animator",
                    lang,
                    "dependencies.missing_error",
                    missing="、".join(report.missing) or "manim、ffmpeg",
                ),
                recoverable=False,
            )
            return ""
        outcome = await run_animation(
            ctx,
            bus,
            AnimationRequest(
                service=service,
                client=client,
                mc=mc,
                mounted=mounted,
                base_history=base_history,
                question=question,
                style_hint=style_hint,
            ),
        )
        if outcome.body:
            await bus.emit_content_done(full_text=outcome.body)
        return outcome.body

    # ---- 装配 ----

    def _stage(self, key: str) -> Stage:
        return next(stage for stage in self.manifest.stages if stage.key == key)

    @staticmethod
    def _stage_deps(
        ctx: UnifiedContext,
        stage: Stage,
        mounted: MountedTools,
        client: LLMClient,
        mc: ModelConfig,
    ) -> LoopDeps:
        """每阶段一份新的 LoopDeps（预算会被循环就地扣减，不能跨阶段复用）。"""
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
