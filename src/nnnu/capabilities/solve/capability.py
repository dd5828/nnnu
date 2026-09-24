"""deep_solve 能力（§7.3）：planning → reasoning → writing 三阶段流水线。

一个回合跑三段，每段一次独立的 Agent 循环（`run_agent_loop`）：
- 每段开头 `emit_status`（前端据此点亮步骤条）+ 一段 markdown 小标题；
- 每段一份**新的 LoopDeps**——`deps.token_budget` 会被循环就地扣减，跨阶段复用会把
  第二段的预算吃完（每段的轮数/输出上限取自 manifest 的 Stage，不是 config）；
- 段落之间靠「把上一阶段产出塞进下一阶段的输入」传递上下文（'--' 不用同一段会话，
  三段各有各的系统提示，职责才不会互相污染）。

不采纳上游 DeepTutor 的「chat 自由循环 + solve_plan/solve_finish_step 工具」那套：
工具驱动的计划表对用户是隐形的，§7.3 要的是三阶段进度条与分段产出。

收尾：`content_done(合并全文)` + `cost_summary` + `done(response=合并全文)`。合并全文
与界面上流式累积的那份逐字一致，也是 `_persist_assistant` 落库的那份。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from nnnu.capabilities._shared import (
    MountedTools,
    append_user_text,
    build_user_message,
    mount_tools,
    session_history,
)
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest, Stage
from nnnu.core.events import StreamEvent
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import ModelConfig, create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

MODES = ("full", "hint")

# 每阶段挂哪些工具（与设置里的开关求交，见 mount_tools）：
# 规划要翻资料，推导要算要深挖，书写只动笔——写的时候还能挂工具就容易写跑偏
STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "planning": (
        "rag",
        "attachment_search",
        "web_search",
        "paper_search",
        "web_fetch",
        "github",
        "brainstorm",
        "ask_user",
    ),
    "reasoning": (
        "rag",
        "attachment_search",
        "reason",
        "code_execution",
        "exec",
        "list_workspace",
        "read_workspace_file",
        "write_workspace_file",
        "ask_user",
    ),
    "writing": ("ask_user",),
}


class _StageBus:
    """阶段总线：原样转发给真实 bus，只把 content_done 换成「累积全文」。

    循环每跑完一段会发 `content_done(本段正文)`，前端按**全文覆盖**处理（§6.1），
    照原样发出去会把前几段从界面上顶掉。这里把前缀（前面几段 + 本段小标题）补上，
    界面上的正文因此只会变长、不会回跳。
    """

    def __init__(self, bus: StreamBus, prefix: Callable[[], str]) -> None:
        self._bus = bus
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bus, name)

    async def emit_content_done(self, *, full_text: str) -> StreamEvent:
        return await self._bus.emit_content_done(full_text=f"{self._prefix()}{full_text}")


class SolveCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="deep_solve",
        version="1.0.0",
        stages=[
            Stage(
                key="planning", label_i18n="stages.planning.label", max_rounds=6, max_tokens=2048
            ),
            Stage(
                key="reasoning", label_i18n="stages.reasoning.label", max_rounds=10, max_tokens=4096
            ),
            Stage(key="writing", label_i18n="stages.writing.label", max_rounds=4, max_tokens=4096),
        ],
        config_schema={
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "default": "full",
                "description": "full = 完整解答；hint = 只给引导不给答案",
            }
        },
        default_model_role="chat",
    )

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        mode = str(ctx.config.get("mode") or "full")
        if mode not in MODES:
            # §9.2：非法组合报 error(recoverable=false)，不去猜用户想要哪种模式
            await bus.emit_error(
                message=f"非法解题模式 {mode!r}：只支持 {'、'.join(MODES)}", recoverable=False
            )
            return

        prompts = get_prompt_manager()
        lang = ctx.language
        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        common = {
            "language": {"zh": "中文", "en": "English"}.get(lang, lang),
            "tools": mounted.tool_lines(),
            "kb_note": (
                prompts.render("deep_solve", lang, "kb_note", kbs=", ".join(mounted.kb_names))
                if mounted.kb_names
                else ""
            ),
            "hint_note": prompts.render("deep_solve", lang, "hint_note") if mode == "hint" else "",
        }
        preamble = prompts.render("deep_solve", lang, "system", **common)

        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        provider_id, model = mc.provider_id, mc.model
        client = create_client(
            model, provider_id=provider_id, base_url=mc.base_url, api_key=mc.api_key
        )

        # 附件只注入一次（图片要多模态 parts、PDF 要解析缓存，重复注入会重复告警），
        # 各阶段在它后面接自己那段「本阶段任务 / 上一阶段产出」
        base_history = session_history(ctx)
        question = await build_user_message(ctx, bus, provider_id, model)

        pieces: list[str] = []  # 流出去的内容，逐字等于最终落库的正文
        previous_stage = ""
        previous_text = ""
        completed = True
        for stage in self.manifest.stages:
            label = prompts.render("deep_solve", lang, f"stages.{stage.key}.label") or stage.key
            heading = prompts.render("deep_solve", lang, f"stages.{stage.key}.heading") or label
            await bus.emit_status(stage=stage.key, message=label)
            header = f"{'' if not pieces else chr(10) * 2}## {heading}\n\n"
            await bus.emit_content_delta(text=header)
            pieces.append(header)

            stage_system = self._stage_system(prompts, lang, stage.key, mode, common)
            history = [
                {"role": "system", "content": f"{preamble}\n\n{stage_system}"},
                *base_history,
                append_user_text(
                    question,
                    self._stage_input(prompts, lang, stage.key, previous_stage, previous_text),
                ),
            ]
            deps = self._stage_deps(
                ctx, stage, mounted.filtered(STAGE_TOOLS[stage.key]), client, mc
            )
            # _StageBus 全部方法转发给真实 bus（鸭子类型），只有 content_done 换成累积版
            outcome = await run_agent_loop(
                ctx,
                _StageBus(bus, lambda: "".join(pieces)),  # type: ignore[arg-type]
                deps,
                history,
            )
            pieces.append(outcome.final_text or "")
            completed = outcome.completed
            if not completed:
                # 循环自己发过 error 了：就此收尾，别硬跑到书写阶段去
                logger.warning("deep_solve 阶段 %s 未正常完成，就此收尾", stage.key)
                break
            previous_stage, previous_text = label, outcome.final_text or ""

        # 累积全文：界面上看到的就是这一份，落库的也是这一份（取最后一个 content_done）
        combined = "".join(pieces)
        if completed:
            await bus.emit_content_done(full_text=combined)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if completed and not bus.terminal_emitted:
            await bus.emit_done(response=combined)

    @staticmethod
    def _stage_system(prompts: Any, lang: str, key: str, mode: str, common: dict[str, Any]) -> str:
        """阶段系统提示：提示模式下的书写阶段换用 system_hint（只给引导不给答案）。"""
        if key == "writing" and mode == "hint":
            return prompts.render("deep_solve", lang, "stages.writing.system_hint", **common)
        return prompts.render("deep_solve", lang, f"stages.{key}.system", **common)

    @staticmethod
    def _stage_input(
        prompts: Any, lang: str, key: str, previous_stage: str, previous_text: str
    ) -> str:
        """本阶段接在用户消息后面的那一段：首段只有任务，后续段带上一阶段的产出。"""
        task = prompts.render("deep_solve", lang, f"stages.{key}.task")
        template_key = "first" if not previous_text else "handoff"
        return prompts.render(
            "deep_solve",
            lang,
            f"stage_input.{template_key}",
            stage_task=task,
            prev_stage=previous_stage,
            prev_output=previous_text,
        )

    @staticmethod
    def _stage_deps(
        ctx: UnifiedContext, stage: Stage, mounted: MountedTools, client: LLMClient, mc: ModelConfig
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
