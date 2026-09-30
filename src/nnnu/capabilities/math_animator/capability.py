"""math_animator 能力（§7.8）：Manim 数学动画视频。

刻意做成薄壳：整条流水线在 `services/render/animator.py`（visualize 的 manim 支线共用），
这里只负责进场条件与收尾：
- **依赖体检放在最前**：缺 manim / ffmpeg 就报友好 error（安装指引），0 次 LLM 调用、
  不崩溃（§7.8 验收「未装依赖时返回友好错误而非 500」）；
- quality 配置非法按 §9.2 报 error(recoverable=false)，不去猜；
- 收尾：正文（artifact 围栏 + 源码 + 日志节选）→ cost_summary → done；
  渲染失败也在正文里说明（流水线的决定），阶段中断由被转发的 error/stopped 兜底。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nnnu.capabilities._shared import build_user_message, mount_tools, session_history
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.render.animator import ANIMATION_STAGES, AnimationRequest, run_animation
from nnnu.services.render.models import QUALITIES
from nnnu.services.render.service import DependencyReport, get_render_service

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)


class MathAnimatorCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="math_animator",
        version="1.0.0",
        # 六阶段直接引用流水线的声明，界面与后端认识同一套（StageBar 还用「声明 ∪ 观测」兜底）
        stages=list(ANIMATION_STAGES),
        config_schema={
            "quality": {
                "type": "string",
                "enum": list(QUALITIES),
                "default": "medium",
                "description": "渲染质量档：低清最快（预览用），高清最慢（成品用）",
            },
            "style_hint": {"type": "string", "description": "附加风格说明（可选）"},
        },
        default_model_role="chat",
    )

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        prompts = get_prompt_manager()
        lang = ctx.language
        quality = str(ctx.config.get("quality") or "medium").strip().lower()
        if quality not in QUALITIES:
            await bus.emit_error(
                message=f"非法渲染质量档 {quality!r}：支持 {'、'.join(QUALITIES)}",
                recoverable=False,
            )
            return

        # ---- 依赖体检：不通过就不花任何 LLM 调用 ----
        try:
            service = get_render_service()
            report = service.available()
        except RuntimeError:
            logger.warning("渲染服务未装配，按依赖缺失处理")
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
            return

        # ---- 装配本回合的模型、挂载工具与用户消息 ----
        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        client = create_client(
            model=mc.model, provider_id=mc.provider_id, base_url=mc.base_url, api_key=mc.api_key
        )
        question = await build_user_message(ctx, bus, mc.provider_id, mc.model)

        outcome = await run_animation(
            ctx,
            bus,
            AnimationRequest(
                service=service,
                client=client,
                mc=mc,
                mounted=mounted,
                base_history=session_history(ctx),
                question=question,
                quality=quality,
                style_hint=str(ctx.config.get("style_hint") or "").strip(),
            ),
        )

        if outcome.body:
            await bus.emit_content_done(full_text=outcome.body)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if outcome.body and not bus.terminal_emitted:
            await bus.emit_done(response=outcome.body)
