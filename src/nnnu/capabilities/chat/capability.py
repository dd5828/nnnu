"""chat 能力（§7.1）：单阶段自由循环。

职责：组装系统提示词/会话历史/当前消息（含附件注入）→ 解析模型与工具 →
跑 Agent 循环 → cost_summary + done 收尾。

挂载工具、拼会话历史、附件注入这几件"每个能力都要做"的事在
`capabilities/_shared.py`（solve 能力共用一份，别在这儿改私有副本）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nnnu.capabilities._shared import (
    build_user_message,
    mount_tools,
    resolve_kb_names,
    session_history,
)
from nnnu.capabilities.chat.personas import resolve_persona_text
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.reasoning import build_reasoning_kwargs

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)


class ChatCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="chat",
        version="1.0.0",
        stages=[],  # 空 = 单阶段自由循环（§6.4）
        config_schema={},
        default_model_role="chat",
    )

    # 共享实现的老入口（测试与调试时按能力取，语义不变）
    _resolve_kb_names = staticmethod(resolve_kb_names)
    _session_history = staticmethod(session_history)

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        await bus.emit_status(stage="chat")

        # 1) 工具挂载与双语描述解析（描述键 → 实际文案）
        mounted = mount_tools(ctx)
        prompts = get_prompt_manager()
        persona_text = ""
        if ctx.persona is not None:
            persona_text = resolve_persona_text(
                persona_id=ctx.persona.id,
                description=ctx.persona.description,
                prompts=prompts,
                lang=ctx.language,
            )
        # 知识库（§7.9）：会话选中的库 → 库名→id 映射，名字同时进系统提示与 rag 的 enum
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        kb_note = (
            prompts.render("chat", ctx.language, "kb_note", kbs=", ".join(mounted.kb_names))
            if mounted.kb_names
            else ""
        )
        system_prompt = prompts.render(
            "chat",
            ctx.language,
            "system",
            tools=mounted.tool_lines(),
            language={"zh": "中文", "en": "English"}.get(ctx.language, ctx.language),
            persona=persona_text,
            kb_note=kb_note,
        )

        # 2) 模型解析（§7.19：settings > 请求覆盖 > env > 默认；附件注入需要 provider/model 判断视觉能力）
        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        provider_id, model = mc.provider_id, mc.model

        # 3) 历史组装：system + 会话历史 + 当前用户消息（含附件注入，§7.1）
        history = [{"role": "system", "content": system_prompt}]
        history.extend(session_history(ctx))
        history.append(await build_user_message(ctx, bus, provider_id, model))

        # 4) 客户端
        client = create_client(
            model, provider_id=provider_id, base_url=mc.base_url, api_key=mc.api_key
        )

        # 5) 循环依赖
        config = ctx.config
        deps = LoopDeps(
            client=client,
            tools=mounted.tool_set(),
            model=model,
            provider=provider_id or "",
            max_rounds=config.get("max_rounds", 20),
            max_output_tokens=config.get("max_output_tokens", 4096),
            token_budget=config.get("token_budget", 32000),
            temperature=config.get("temperature", mc.temperature),
            reasoning_effort=config.get("reasoning_effort", mc.reasoning_effort),
            thinking_extra=build_reasoning_kwargs(
                provider_id=provider_id or "",
                model=model,
                reasoning_effort=config.get("reasoning_effort"),
            ),
        )

        # 6) 循环 + 统一收尾（§6.1：cost_summary + done；error 由循环发出）
        outcome = await run_agent_loop(ctx, bus, deps, history)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if outcome.completed and not bus.terminal_emitted:
            await bus.emit_done(
                response=outcome.final_text,
                citations=outcome.citations,
                tool_calls=outcome.tool_calls,
            )
