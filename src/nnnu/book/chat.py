"""页聊天（§7.14）：读这一页有问题就地问，回答接地到本页与书的素材来源。

装配照 co_writer/edit.py（伪 UnifiedContext + mount_tools + 裸 StreamBus + run_agent_loop），
差异在内容面：系统提示词 = book.yaml page_chat + 本页块正文 + 章来源清单；工具只留
rag（读这本书勾过的库）与 web_fetch（抓正文里给出的链接），不开联网搜索。
- 页聊**不落** sessions/messages/L1 记忆（会话 id 是伪的 book-<page_id>），
  历史自存 book_page_messages（读写都在服务层，这里只管拼提示词与会话历史）；
- 勾了库但一个都没就绪 → degraded="kb_not_ready"，如实降级不装接地；
- 用量计在 unified.cost，调用方按合成 turn_id（bookchat-<msg_id>）记账。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nnnu.book.export import block_markdown
from nnnu.book.models import Block, BookPage, Chapter
from nnnu.book.spine import normalize_language
from nnnu.capabilities._shared import mount_tools
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.context import KbRef, SessionRef, UnifiedContext
from nnnu.core.ids import new_id
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import ToolMountFlags
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.sessions.models import Message

PAGE_CHAT_MAX_ROUNDS = 6  # 找回资料再答，6 轮绰绰有余；防跑飞
PAGE_CHAT_MAX_OUTPUT_TOKENS = 2048
PAGE_CHAT_TOKEN_BUDGET = 16000
PAGE_CHAT_HISTORY = 6  # 历史窗口：最近 3 轮往来（服务层截取）
PAGE_CHAT_BLOCKS_CHARS = 8000  # 本页块正文进提示词的上限（动画源码很长，别整页塞）
ALLOWED_TOOLS = ("rag", "web_fetch")

_NO_SOURCES = {"zh": "（这一章没有标注来源）", "en": "(no sources marked for this chapter)"}


class BookChatOutputError(LLMError):
    """这轮回复没跑完或回复为空——路由按 502（照 CoWriterOutputError 手法）。"""


@dataclass(slots=True)
class ChatOutcome:
    answer: str
    citations: list[dict[str, Any]]
    degraded: str | None  # 接地降级标记（如 kb_not_ready）；None = 一切正常
    model: str
    usage: dict[str, Any]  # cost.summary() 形状：{tokens, cost, per_model}


def _clip(text: str, limit: int) -> str:
    clean = text.strip()
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


def _page_text(blocks: list[Block], lang: str) -> str:
    """本页 done 块的正文拼一段（含测验答案——助手该知道答案）。"""
    parts = []
    for block in blocks:
        if block.status != "done":
            continue
        text = block_markdown(block, lang, include_answers=True)
        if text:
            parts.append(text)
    return _clip("\n\n".join(parts), PAGE_CHAT_BLOCKS_CHARS)


def _sources_text(chapter: Chapter, lang: str) -> str:
    lines = [
        f"- [{ref.kind}] {ref.label or ref.ref}"
        for ref in chapter.source_refs
        if ref.ref or ref.label
    ]
    return "\n".join(lines) or _NO_SOURCES.get(lang, _NO_SOURCES["zh"])


async def run_page_chat(
    *,
    page: BookPage,
    chapter: Chapter,
    question: str,
    history: list[dict[str, str]],
    language: str,
    kb_ids: list[str],
) -> ChatOutcome:
    """跑一拍页聊（history 是最近几条往来，已由调用方截好窗口）。"""
    lang = normalize_language(language)
    prompts = get_prompt_manager()

    # 1) 伪上下文 + 工具挂载（勾了库才挂 rag——它是 CONTEXT_GATED，靠
    # tool_flags.context 开闸；web_fetch 恒挂，allowlist 只是收窄）
    unified = UnifiedContext(
        session=SessionRef(id=f"book-{page.id}"),
        capability="book",
        message=Message.new(session_id=f"book-{page.id}", role="user", content=question),
        kb_refs=[KbRef(kb_id=kb_id) for kb_id in kb_ids],
        tool_flags=ToolMountFlags(context={"rag"} if kb_ids else set()),
        language=lang,
    )
    mounted = mount_tools(unified, allowlist=ALLOWED_TOOLS)
    unified.metadata["rag_kbs"] = mounted.rag_kbs
    degraded: str | None = None
    if kb_ids and not mounted.kb_names:
        degraded = "kb_not_ready"  # 勾了库但一个都没就绪：这轮没接地，如实降级

    # 2) 提示词拼装
    kb_note = (
        prompts.render("book", lang, "page_chat.kb_note", kbs=", ".join(mounted.kb_names))
        if mounted.kb_names
        else ""
    )
    system_prompt = prompts.render(
        "book",
        lang,
        "page_chat.system",
        tools=mounted.tool_lines(),
        kb_note=kb_note,
        chapter_title=chapter.title,
        blocks=_page_text(page.blocks, lang) or "（这一页还没有生成内容）",
        sources=_sources_text(chapter, lang),
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": prompts.render("book", lang, "page_chat.user", question=question),
        }
    )

    # 3) 模型与循环
    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    deps = LoopDeps(
        client=client,
        tools=mounted.tool_set(),
        model=model_config.model,
        provider=model_config.provider_id or "",
        max_rounds=PAGE_CHAT_MAX_ROUNDS,
        max_output_tokens=PAGE_CHAT_MAX_OUTPUT_TOKENS,
        token_budget=PAGE_CHAT_TOKEN_BUDGET,
        temperature=model_config.temperature,
        reasoning_effort=model_config.reasoning_effort,
        thinking_extra=build_reasoning_kwargs(
            provider_id=model_config.provider_id or "",
            model=model_config.model,
            reasoning_effort=model_config.reasoning_effort,
        ),
    )
    bus = StreamBus(turn_id=new_id("turn"))
    outcome = await run_agent_loop(unified, bus, deps, messages)

    # 4) 结果
    if not outcome.completed:
        raise BookChatOutputError("这轮回复没跑完（模型调用出错），请重试")
    answer = outcome.final_text.strip()
    if not answer:
        raise BookChatOutputError("模型没有给出回复，请重试")
    return ChatOutcome(
        answer=answer,
        citations=outcome.citations,
        degraded=degraded,
        model=model_config.model,
        usage=unified.cost.summary(),
    )
