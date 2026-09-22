"""chat 能力（§7.1）：单阶段自由循环。

职责：组装系统提示词/会话历史/当前消息（含附件注入）→ 解析模型与工具 →
跑 Agent 循环 → cost_summary + done 收尾。全量规则（persona/附件/引用）P2 补齐。
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nnnu.capabilities.chat.personas import resolve_persona_text
from nnnu.core.agent_loop import LoopDeps, ToolSet, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest
from nnnu.core.stream_bus import StreamBus
from nnnu.runtime import home
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.provider_registry import build_registry, find_by_id, find_model
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.sessions.models import Message

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

# 附件注入上限（§7.1：注入是给模型直读的摘要，全量检索走 attachment_search）
PAGE_INJECT_MAX_CHARS = 2000  # 每页注入上限
ATTACHMENT_INJECT_MAX_CHARS = 12000  # 每附件注入总上限
HISTORY_REF_INJECT_MAX_CHARS = 6000  # 每个引用会话的转录注入上限
HISTORY_REF_MESSAGE_MAX_CHARS = 2000  # 引用会话中单条消息上限


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

        # 2) 模型解析（§7.19：settings > 请求覆盖 > env > 默认；附件注入需要 provider/model 判断视觉能力）
        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        provider_id, model = mc.provider_id, mc.model

        # 3) 历史组装：system + 会话历史 + 当前用户消息（含附件注入，§7.1）
        history = [{"role": "system", "content": system_prompt}]
        history.extend(self._session_history(ctx))
        history.append(await self._build_user_message(ctx, bus, provider_id, model))

        # 4) 客户端
        client = create_client(
            model, provider_id=provider_id, base_url=mc.base_url, api_key=mc.api_key
        )

        # 5) 循环依赖
        config = ctx.config
        deps = LoopDeps(
            client=client,
            tools=ToolSet(mounted, descriptions),
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

    @staticmethod
    def _session_history(ctx: UnifiedContext) -> list[dict]:
        """会话历史 → OpenAI 消息格式（§6.8；含上次 assistant 的思考与工具轨迹）。

        当前消息按 id 过滤（编排器可能已落库），由调用方经 _build_user_message 追加。
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
        return history

    async def _build_user_message(
        self, ctx: UnifiedContext, bus: StreamBus, provider_id: str | None, model: str
    ) -> dict:
        """当前用户消息：文本附件带页码标记注入正文，图片走视觉模型（多模态 parts）。

        同时把解析页装进 ctx.metadata["attachment_index"] 供 attachment_search 检索。
        附件解析缓存缺失/失败 → warning 跳过，不阻断回合（§7.1 解析缓存有清理入口）。
        """
        attachment_texts: list[str] = []
        image_parts: list[dict] = []
        index_entries: list[dict[str, Any]] = []
        for attachment in ctx.attachments:
            directory = home.get_data_root() / "user" / "uploads" / ctx.session.id / attachment.id
            parsed_path = directory / "parsed.json"
            if not parsed_path.is_file():
                await bus.emit_warning(message=f"附件《{attachment.name}》解析缓存缺失，已忽略")
                continue
            try:
                parsed: dict[str, Any] = json.loads(parsed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                await bus.emit_warning(message=f"附件《{attachment.name}》解析缓存损坏，已忽略")
                continue
            if parsed.get("kind") == "image":
                await self._inject_image(
                    ctx, bus, attachment, directory, image_parts, provider_id, model
                )
            elif not parsed.get("ok"):
                await bus.emit_warning(
                    message=f"附件《{attachment.name}》解析失败（{parsed.get('error', '未知')}），已忽略"
                )
            else:
                pages = parsed.get("pages") or []
                if pages:
                    index_entries.append(
                        {"id": attachment.id, "name": attachment.name, "pages": pages}
                    )
                    attachment_texts.append(
                        _format_paged_attachment(attachment.name, pages, ctx.language)
                    )
                elif parsed.get("text"):
                    text = str(parsed["text"])[:ATTACHMENT_INJECT_MAX_CHARS]
                    attachment_texts.append(f"【附件《{attachment.name}》】\n{text}")
        if index_entries:
            ctx.metadata["attachment_index"] = index_entries

        content_text = ctx.message.content
        # 一次性引用：历史会话转录（§7.1；笔记本/题库/书页随 P9/P10 实体扩展）
        ref_texts = [
            _format_history_ref(ref, ctx.language)
            for ref in ctx.metadata.get("history_ref_transcripts", [])
        ]
        injected = [*attachment_texts, *ref_texts]
        if injected:
            content_text += "\n\n" + "\n\n".join(injected)
        if image_parts:
            # 多模态 parts：文本在前、图片在后（OpenAI 兼容格式）
            return {
                "role": "user",
                "content": [{"type": "text", "text": content_text}, *image_parts],
            }
        return {"role": "user", "content": content_text}

    async def _inject_image(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        attachment,
        directory: Path,
        image_parts: list[dict],
        provider_id: str | None,
        model: str,
    ) -> None:
        """图片附件 → base64 data URI 多模态 part；模型不支持视觉时 warning 跳过。"""
        if not _supports_vision(provider_id, model):
            await bus.emit_warning(
                message=f"图片附件《{attachment.name}》已忽略：当前模型不支持视觉输入"
            )
            return
        originals = sorted(directory.glob("original.*"))
        if not originals:
            await bus.emit_warning(message=f"图片附件《{attachment.name}》文件缺失，已忽略")
            return
        raw = originals[0].read_bytes()
        mime = attachment.mime or "image/png"
        image_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"},
            }
        )


def _supports_vision(provider_id: str | None, model: str) -> bool:
    spec = find_by_id(build_registry(), provider_id) if provider_id else None
    info = find_model(spec, model) if spec else None
    return info is not None and "vision" in info.capabilities


def _format_history_ref(ref: dict[str, Any], lang: str) -> str:
    """引用会话转录（截断，仅 user/assistant 消息，思考与工具轨迹省略）。"""
    header = (
        f"【引用历史会话《{ref.get('title') or ref.get('id')}》】"
        if lang == "zh"
        else f'[Referenced history session "{ref.get("title") or ref.get("id")}"]'
    )
    role_labels = (
        {"user": "用户", "assistant": "助手"}
        if lang == "zh"
        else {
            "user": "User",
            "assistant": "Assistant",
        }
    )
    lines: list[str] = [header]
    budget = HISTORY_REF_INJECT_MAX_CHARS
    for message in ref.get("messages", []):
        role = message.get("role")
        if role not in role_labels or not message.get("content"):
            continue
        if budget <= 0:
            lines.append("…" if lang == "zh" else "...")
            break
        line = f"{role_labels[role]}：{str(message['content'])[:HISTORY_REF_MESSAGE_MAX_CHARS]}"
        lines.append(line[:budget])
        budget -= len(line)
    return "\n".join(lines)


def _format_paged_attachment(name: str, pages: list[dict[str, Any]], lang: str) -> str:
    """PDF 附件按页注入（带页码标记，模型可引用页码；超限截断并提示走检索）。"""
    page_label = "第{n}页" if lang == "zh" else "Page {n}"
    header = (
        f"【附件《{name}》内容，共 {len(pages)} 页（引用时请标注页码）】"
        if lang == "zh"
        else f'[Attachment "{name}", {len(pages)} pages (cite the page number when referencing)]'
    )
    truncation_note = (
        "（内容过长已截断，可用 attachment_search 检索更多段落）"
        if lang == "zh"
        else "(content truncated; use attachment_search for more passages)"
    )
    lines = [header]
    budget = ATTACHMENT_INJECT_MAX_CHARS
    truncated = False
    for page in pages:
        text = str(page.get("text", "")).strip()
        if not text:
            continue
        if budget <= 0:
            truncated = True
            break
        chunk = f"[{page_label.format(n=page.get('page'))}]\n{text[:PAGE_INJECT_MAX_CHARS]}"
        lines.append(chunk[:budget])
        budget -= len(chunk)
    if truncated or budget <= 0:
        lines.append(truncation_note)
    return "\n\n".join(lines)
