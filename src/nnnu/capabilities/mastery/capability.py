"""mastery_path 能力（§7.5）：**门就是游标** + 八工具 + 自由循环。

对外只有一个阶段 `responding`（§6.4 明令：阶段是能力的对外契约，内部不分段）：

1. **解析路径**（零 LLM）：`config.path_id` → 本会话绑定的路径。一条都没有不算错——
   那一回合的活就是建树（`mastery_build`）或切到已有的（`mastery_switch`）；
   只有「指名道姓给的 id 找不到」才打回。
2. **自由循环**：`run_agent_loop` 里模型讲解、调 mastery 八工具。
   **状态由模型自己取**：提示词要求每轮第一条先调 `mastery_status`，
   服务端不再往 system 提示词里塞状态块（这是上游的用法，
   见 `deeptutor/capabilities/mastery/prompts/{zh,en}/system.md`）。
3. **ask_user 缝**（`ask_seam.py`）：发卡前把卡面归位到库里那道题，收到答复先把**学习者原话**
   落库再回给模型——判分只认那一列，模型转述无效。

**本能力零 LLM 调用**（除了循环本身），每回合步数可预测。

两处与其他分段能力不同的地方（照旧）：
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
from nnnu.capabilities.mastery import ask_seam
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest, Stage
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.learning.service import get_learning_service
from nnnu.services.llm.factory import ModelConfig, create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

# 配置项（键名不能动：tests/test_plugins_api.py 断言了 path_id / practice_count）
# practice_count 是出题权归服务端时的遗留：现在题目由模型现场出，这个数不再驱动任何东西，
# 但配置项是对外契约（§9.2），manifest 里的键留着，校验也留着。
PRACTICE_COUNT_MIN = 1
PRACTICE_COUNT_MAX = 5
PRACTICE_COUNT_DEFAULT = 3

# 本阶段挂哪些工具：八件（读/出题/判分/定性判定/建/列/切/脱离）+ 查资料 + 澄清 + 执行。
# brainstorm / reason / consult_subagent 排除在外（它们自己会发 LLM 调用）。
STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "responding": (
        "mastery_status",
        "mastery_quiz",
        "mastery_grade",
        "mastery_assess",
        "mastery_build",
        "mastery_paths",
        "mastery_switch",
        "mastery_leave",
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
                    "或让模型用 mastery_paths / mastery_build 重建一条。"
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

        # 卡面归位 + 原话落库：模型自己发卡（三段式的第二段），这两个口子得在这儿堵。
        # 提示词里**不留状态块**：状态由模型每轮第一条 mastery_status 自己取（上游的用法）
        ask_seam.install(ctx, bus, service)

        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        client = create_client(
            mc.model, provider_id=mc.provider_id, base_url=mc.base_url, api_key=mc.api_key
        )
        system = prompts.render(
            "mastery",
            lang,
            "system",
            language={"zh": "中文", "en": "English"}.get(lang, lang),
            tools=mounted.tool_lines(),
            kb_note=(
                prompts.render("mastery", lang, "kb_note", kbs="、".join(mounted.kb_names))
                if mounted.kb_names
                else ""
            ),
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
