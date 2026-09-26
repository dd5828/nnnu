"""能力之间的公共装配件：挂载工具、拼历史、注入附件。

chat 与 solve 两处都要这几件事，抽出来放这里，别再各写一份（附件注入尤其
不能抄两遍：解析缓存的路径、截断上限、视觉降级提示都得是同一套）。

历史与会话历史的细节见 chat 能力原先的注释（原样搬过来）：
- 会话历史只回放纯文本，**不回放 tool_calls**（落库的那份是给界面看的轨迹，
  形状不是线格式；OpenAI 契约里带 tool_calls 的 assistant 后面必须紧跟
  role="tool" 应答，照原样回放会被上游 422）；
- 附件解析缓存缺失/失败一律 warning 跳过，不阻断回合（§7.1）。
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection

from nnnu.core.agent_loop import ToolSet
from nnnu.core.stream_bus import StreamBus
from nnnu.runtime import home
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.protocol import LLMClient, LLMRequest
from nnnu.services.llm.provider_registry import build_registry, find_by_id, find_model
from nnnu.services.sessions.models import Message

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

# 附件注入上限（§7.1：注入是给模型直读的摘要，全量检索走 attachment_search）
PAGE_INJECT_MAX_CHARS = 2000  # 每页注入上限
ATTACHMENT_INJECT_MAX_CHARS = 12000  # 每附件注入总上限
HISTORY_REF_INJECT_MAX_CHARS = 6000  # 每个引用会话的转录注入上限
HISTORY_REF_MESSAGE_MAX_CHARS = 2000  # 引用会话中单条消息上限


@dataclass(slots=True)
class MountedTools:
    """本回合实际挂上的工具及其配套（描述文案、知识库名与映射）。"""

    tools: dict[str, Any] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    kb_names: list[str] = field(default_factory=list)
    rag_kbs: dict[str, str] = field(default_factory=dict)

    def tool_lines(self) -> str:
        """给系统提示词用的「名字（说明）」清单。"""
        return ", ".join(f"{name}（{desc}）" for name, desc in self.descriptions.items()) or "无"

    def filtered(self, allowlist: Collection[str]) -> "MountedTools":
        """只留白名单里的工具（阶段能力分阶段收窄用；描述与库名照搬，不重新解析）。"""
        allowed = set(allowlist)
        return MountedTools(
            tools={name: tool for name, tool in self.tools.items() if name in allowed},
            descriptions={
                name: desc for name, desc in self.descriptions.items() if name in allowed
            },
            kb_names=self.kb_names,
            rag_kbs=self.rag_kbs,
        )

    def tool_set(self) -> ToolSet:
        """循环用的工具视图：rag 的 kb_name 收口成本回合挂载的库名（模型没得猜）。"""
        return ToolSet(
            self.tools,
            self.descriptions,
            parameter_overrides={"rag": {"properties": {"kb_name": {"enum": self.kb_names}}}},
        )


def mount_tools(ctx: UnifiedContext, *, allowlist: Collection[str] | None = None) -> MountedTools:
    """按工具开关挂载工具（§6.3），可选白名单收窄（阶段能力按阶段取子集，见 filtered）。

    工具描述文案取 `prompts/{lang}/chat.yaml:tool_descriptions.<name>`——工具目录
    是全站共用的那一份，solve 不另抄一套（与提示词 YAML 的 `system` 各管各的）。
    """
    from nnnu.runtime.registry.tool_registry import get_tool_registry

    mounted = get_tool_registry().mounted(ctx.tool_flags)
    if allowlist is not None:
        allowed = set(allowlist)
        mounted = {name: tool for name, tool in mounted.items() if name in allowed}
    prompts = get_prompt_manager()
    descriptions = {
        name: prompts.render("chat", ctx.language, f"tool_descriptions.{name}")
        or tool.definition.description
        for name, tool in mounted.items()
    }
    kb_names, rag_kbs = resolve_kb_names(ctx)
    return MountedTools(
        tools=mounted, descriptions=descriptions, kb_names=kb_names, rag_kbs=rag_kbs
    )


def resolve_kb_names(ctx: UnifiedContext) -> tuple[list[str], dict[str, str]]:
    """会话选中的知识库 →（库名列表，保持选择顺序；库名 → kb_id 映射）。

    只认 ready 且有活跃版本的库（删了/没建完的一律当没选，fail-closed）；
    重名时后选中的覆盖前者，列表里只留一个。
    """
    from nnnu.services.knowledge.service import get_kb_service
    from nnnu.services.knowledge.types import KB_READY

    if not ctx.kb_refs:
        return [], {}
    try:
        service = get_kb_service()
    except RuntimeError:
        return [], {}
    names: list[str] = []
    mapping: dict[str, str] = {}
    for ref in ctx.kb_refs:
        try:
            manifest = service.get_kb(ref.kb_id)
        except Exception:
            logger.warning("知识库 %s 解析失败，按未挂载处理", ref.kb_id, exc_info=True)
            continue
        if manifest is None or manifest.status != KB_READY or manifest.active_version <= 0:
            continue
        if manifest.name not in mapping:
            names.append(manifest.name)
        mapping[manifest.name] = ref.kb_id
    return names, mapping


def session_history(ctx: UnifiedContext) -> list[dict]:
    """会话历史 → OpenAI 消息格式（§6.8；只回放纯文本，不回放工具轨迹）。

    当前消息按 id 过滤（编排器可能已落库），由调用方经 build_user_message 追加。
    """
    history: list[dict] = []
    messages: list[Message] = ctx.metadata.get("session_messages", [])
    for message in messages:
        if message.id == ctx.message.id:
            continue
        if message.role == "user":
            history.append({"role": "user", "content": message.content})
        elif message.role == "assistant" and message.content:
            # 纯工具回合的 assistant 没有正文，回放空 content 会被上游判非法
            history.append({"role": "assistant", "content": message.content})
    return history


async def build_user_message(
    ctx: UnifiedContext, bus: StreamBus, provider_id: str | None, model: str
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
            await _inject_image(ctx, bus, attachment, directory, image_parts, provider_id, model)
        elif not parsed.get("ok"):
            await bus.emit_warning(
                message=f"附件《{attachment.name}》解析失败（{parsed.get('error', '未知')}），已忽略"
            )
        else:
            pages = parsed.get("pages") or []
            if pages:
                index_entries.append({"id": attachment.id, "name": attachment.name, "pages": pages})
                attachment_texts.append(
                    format_paged_attachment(attachment.name, pages, ctx.language)
                )
            elif parsed.get("text"):
                text = str(parsed["text"])[:ATTACHMENT_INJECT_MAX_CHARS]
                attachment_texts.append(f"【附件《{attachment.name}》】\n{text}")
    if index_entries:
        ctx.metadata["attachment_index"] = index_entries

    content_text = ctx.message.content
    # 一次性引用：历史会话转录（§7.1；笔记本/题库/书页随 P9/P10 实体扩展）
    ref_texts = [
        format_history_ref(ref, ctx.language)
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


async def complete_with_cost(
    ctx: UnifiedContext,
    client: LLMClient,
    model_config: ModelConfig,
    *,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """阶段能力内部的一次非流式调用（如出题的审校轮），用量并进本回合成本。

    用能力自己那个 client——重开一个会丢掉脚本化测试的步进；成本记进 `ctx.cost`，
    不是 `ctx.metadata["cost_tracker"]`（那是给工具用的，见 agent_loop 的 ToolContext）。
    """
    response = await client.complete(
        LLMRequest(
            messages=messages,
            model=model_config.model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    )
    if response.usage:
        ctx.cost.add_usage(
            provider=model_config.provider_id or "",
            model=model_config.model,
            input_tokens=int(
                response.usage.get("prompt_tokens") or response.usage.get("input_tokens") or 0
            ),
            output_tokens=int(
                response.usage.get("completion_tokens") or response.usage.get("output_tokens") or 0
            ),
        )
    return response.text or ""


def strip_label_prefix(option: str, position: int) -> str:
    """剥掉模型爱写的选项前缀（"A. 文本" / "A、文本" / "(A) 文本"）。

    出题类能力（deep_question / mastery_path）共用：不剥的话界面上会显示成「A. A. 4」。
    """
    labels = "ABCDEFGH"
    text = option.strip()
    for prefix in (
        f"{labels[position]}.",
        f"{labels[position]}、",
        f"{labels[position]})",
        f"({labels[position]})",
    ):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def append_user_text(message: dict, extra: str) -> dict:
    """在用户消息后面接一段文本（阶段能力用它带上前一阶段产出与阶段指令）。

    多模态 parts 的情况接在首个 text part 上，图片 part 原样保留。
    """
    if not extra:
        return message
    content = message.get("content")
    if isinstance(content, list):
        parts = [dict(part) for part in content]
        for part in parts:
            if part.get("type") == "text":
                part["text"] = f"{part.get('text', '')}\n\n{extra}"
                break
        else:
            parts.insert(0, {"type": "text", "text": extra})
        return {**message, "content": parts}
    return {**message, "content": f"{content or ''}\n\n{extra}"}


async def _inject_image(
    ctx: UnifiedContext,
    bus: StreamBus,
    attachment: Any,
    directory: Path,
    image_parts: list[dict],
    provider_id: str | None,
    model: str,
) -> None:
    """图片附件 → base64 data URI 多模态 part；模型不支持视觉时 warning 跳过。"""
    if not supports_vision(provider_id, model):
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


def supports_vision(provider_id: str | None, model: str) -> bool:
    spec = find_by_id(build_registry(), provider_id) if provider_id else None
    info = find_model(spec, model) if spec else None
    return info is not None and "vision" in info.capabilities


def format_history_ref(ref: dict[str, Any], lang: str) -> str:
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


def format_paged_attachment(name: str, pages: list[dict[str, Any]], lang: str) -> str:
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
