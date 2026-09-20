"""chat 能力（§7.1 的 P1 最小实现）：单阶段自由循环。

职责：组装系统提示词/会话历史/当前消息 → 解析模型与工具 → 跑 Agent 循环 →
cost_summary + done 收尾。全量规则（附件/persona/引用注入等）P2 补齐。
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from nnnu.capabilities.chat.personas import resolve_persona_text
from nnnu.core.agent_loop import LoopDeps, ToolSet, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import create_client, parse_model_ref
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.sessions.models import Message

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

DEFAULT_MODEL_REF = "deepseek:deepseek-chat"  # P1 默认（env NNNU_MODEL 可覆盖）


class ChatCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="chat",
        version="1.0.0",
        stages=[],  # 空 = 单阶段自由循环（§6.4）
        config_schema={},
        default_model_role="chat",
    )

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        from nnnu.runtime.registry.tool_registry import get_tool_registry

        await bus.emit_status(stage="chat")

        # 1) 工具挂载与双语描述解析（描述键 → 实际文案）
        registry = get_tool_registry()
        mounted = registry.mounted(ctx.tool_flags)
        prompts = get_prompt_manager()
        descriptions = {
            name: prompts.render("chat", ctx.language, f"tool_descriptions.{name}")
            or tool.definition.description
            for name, tool in mounted.items()
        }
        tool_lines = ", ".join(f"{name}（{desc}）" for name, desc in descriptions.items()) or "无"
        persona_text = ""
        if ctx.persona is not None:
            persona_text = resolve_persona_text(
                persona_id=ctx.persona.id,
                description=ctx.persona.description,
                prompts=prompts,
                lang=ctx.language,
            )
        system_prompt = prompts.render(
            "chat",
            ctx.language,
            "system",
            tools=tool_lines,
            language={"zh": "中文", "en": "English"}.get(ctx.language, ctx.language),
            persona=persona_text,
        )

        # 2) 历史组装：system + 会话历史 + 当前用户消息（一次性引用注入 P2 补齐）
        history = [{"role": "system", "content": system_prompt}]
        history.extend(self._session_history(ctx))

        # 3) 模型与客户端
        model_ref = ctx.model
        if model_ref is None:
            provider_id, model = parse_model_ref(os.environ.get("NNNU_MODEL") or DEFAULT_MODEL_REF)
        else:
            provider_id, model = model_ref.provider, model_ref.model
        client = create_client(model, provider_id=provider_id)

        # 4) 循环依赖
        config = ctx.config
        deps = LoopDeps(
            client=client,
            tools=ToolSet(mounted, descriptions),
            model=model,
            provider=provider_id or "",
            max_rounds=config.get("max_rounds", 20),
            max_output_tokens=config.get("max_output_tokens", 4096),
            token_budget=config.get("token_budget", 32000),
            temperature=config.get("temperature"),
            reasoning_effort=config.get("reasoning_effort"),
            thinking_extra=build_reasoning_kwargs(
                provider_id=provider_id or "",
                model=model,
                reasoning_effort=config.get("reasoning_effort"),
            ),
        )

        # 5) 循环 + 统一收尾（§6.1：cost_summary + done；error 由循环发出）
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

    @staticmethod
    def _session_history(ctx: UnifiedContext) -> list[dict]:
        """会话历史 → OpenAI 消息格式（§6.8；含上次 assistant 的思考与工具轨迹）。

        当前消息按 id 过滤（编排器可能已落库），避免重复注入。
        """
        history: list[dict] = []
        messages: list[Message] = ctx.metadata.get("session_messages", [])
        for message in messages:
            if message.id == ctx.message.id:
                continue
            if message.role == "user":
                history.append({"role": "user", "content": message.content})
            elif message.role == "assistant":
                entry: dict = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    entry["tool_calls"] = message.tool_calls
                history.append(entry)
        # 当前用户消息（ctx.message）追加在历史之后
        history.append({"role": "user", "content": ctx.message.content})
        return history
