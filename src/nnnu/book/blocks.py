"""块生成（§7.14 编译引擎的心脏）：一个块计划 → 一次 LLM 调用 → 校验过的 payload。

对照上游（page_planner 之后逐块专属提示词 + 多段编排）压成一次调用：系统提示词
（book.yaml 的 blocks.<type>.system）定义输出契约，用户消息给章节信息/素材/聚焦点；
输出洗不干净就带问题重试一次（同 spine，`blocks.repair` 带问题清单回去），两次都
不行抛 BlockGenerationError——调用方把该块标 error 继续编译别的块（坏块不炸整书）。

各型的产出契约：
- JSON 型（callout/quiz/flashcard/timeline/code/deep_dive/concept_graph）：extract_json
  解析 → validate_block_payload 归一；
- text：Markdown 正文（整段被一条围栏包着时剥掉——有的模型爱包 ```markdown）；
- figure / interactive_html：Markdown 正文，第一条围栏必须是对应的渲染围栏，抠出
  代码过 services/render/validation.py 的确定性校验（mermaid 标签禁字符等），不过就
  带着原因重试；
- note：零 LLM，空块交给用户自己写；
- animation：外包给 math_animator 六阶段流水线（run_animation，本地替身渲染也可跑），
  用量并回 tracker。
"""

from __future__ import annotations

import json
import re
from typing import Any

from nnnu.book.llm import BookOutputError, extract_json, one_shot
from nnnu.book.models import (
    FENCE_TAGS,
    OUTPUT_TOKEN_BUDGET,
    BookError,
    Chapter,
    validate_block_payload,
)
from nnnu.book.spine import normalize_language
from nnnu.capabilities._shared import mount_tools
from nnnu.core.context import SessionRef, UnifiedContext
from nnnu.core.ids import new_id
from nnnu.core.stream_bus import StreamBus
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.render.animator import AnimationRequest, run_animation
from nnnu.services.render.service import get_render_service
from nnnu.services.render.validation import extract_render_code, validate_visualization
from nnnu.services.sessions.models import Message

BLOCK_MAX_ATTEMPTS = 2
MAX_TOKENS_SLACK = 512  # 生成按预算放宽一档，防 JSON/围栏收尾被截断
ANIMATION_MATERIAL_CHARS = 2_000  # 动画问题里带多少素材节选

JSON_BLOCK_TYPES = (
    "callout",
    "quiz",
    "flashcard",
    "timeline",
    "code",
    "deep_dive",
    "concept_graph",
)
FENCE_BLOCK_TYPES = ("figure", "interactive_html")

# 围栏语言标记 → 渲染类型（别名同 validation.py 的 FENCE_ALIASES）
_RENDER_TYPE_BY_TAG = {
    "svg": "svg",
    "xml": "svg",
    "echarts": "echarts",
    "json": "echarts",
    "mermaid": "mermaid",
    "html": "html",
}
_FIRST_FENCE_RE = re.compile(r"```[ \t]*([a-zA-Z0-9_-]*)")
_WHOLE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```$", re.DOTALL)


class BlockGenerationError(Exception):
    """两次都没生成出可用块——调用方把块标 error，继续编译别的块。"""


def max_tokens_for(block_type: str) -> int:
    return OUTPUT_TOKEN_BUDGET.get(block_type, 800) + MAX_TOKENS_SLACK


def _clip(text: str, limit: int) -> str:
    clean = (text or "").strip()
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


def _unwrap_whole_fence(text: str) -> str:
    """整段正好被一条围栏包着时剥掉；正文里另围栏（真代码块）就不剥。"""
    stripped = text.strip()
    match = _WHOLE_FENCE_RE.match(stripped)
    if match is None:
        return stripped
    body = match.group("body")
    return body.strip() if "```" not in body else stripped


def fence_reason(block_type: str, markdown: str) -> str | None:
    """figure/interactive_html 的深校验：第一条围栏要对得上，代码要过渲染校验器。"""
    match = _FIRST_FENCE_RE.search(markdown)
    if match is None:
        return "没有找到代码围栏"
    tag = match.group(1).lower()
    render_type = _RENDER_TYPE_BY_TAG.get(tag)
    if render_type not in FENCE_TAGS[block_type]:
        allowed = " / ".join(f"```{item}" for item in FENCE_TAGS[block_type])
        return f"第一条围栏是 ```{tag or '?'}，这里只认 {allowed}"
    code = extract_render_code(markdown, render_type)
    if not code:
        return f"```{tag} 围栏里没有内容"
    ok, reason = validate_visualization(code, render_type)
    return None if ok else f"图示没过渲染校验：{reason}"


def _parse_and_validate(block_type: str, text: str) -> dict[str, Any]:
    """模型输出 → 归一 payload；洗不出来抛 BookOutputError/BookError（重试的抓手）。"""
    if block_type == "text":
        return validate_block_payload(block_type, {"markdown": _unwrap_whole_fence(text)})
    if block_type in FENCE_BLOCK_TYPES:
        payload = validate_block_payload(block_type, {"markdown": text.strip()})
        reason = fence_reason(block_type, payload["markdown"])
        if reason is not None:
            raise BookError(reason, code="invalid_payload")
        return payload
    return validate_block_payload(block_type, extract_json(text))


def _render_user_message(
    chapter: Chapter,
    focus: str,
    material_text: str,
    graph_seed: dict[str, Any] | None,
    block_type: str,
    language: str,
) -> str:
    extra = ""
    if block_type == "concept_graph" and graph_seed and graph_seed.get("nodes"):
        extra = json.dumps(graph_seed, ensure_ascii=False)
    prompts = get_prompt_manager()
    return prompts.render(
        "book",
        language,
        "blocks.common.user",
        chapter_title=chapter.title,
        content_type=chapter.content_type,
        summary=chapter.summary or "（无）",
        objectives="；".join(chapter.objectives) or "（无）",
        material=material_text or "（没有可用素材，按通用知识写）",
        focus=focus or chapter.title,
        extra=extra,
    )


async def generate_block(
    *,
    block_type: str,
    chapter: Chapter,
    focus: str = "",
    material_text: str = "",
    graph_seed: dict[str, Any] | None = None,
    language: str = "zh",
    tracker: CostTracker | None = None,
) -> dict[str, Any]:
    """生成一个块的 payload（校验并归一过）；两次不过抛 BlockGenerationError。"""
    if block_type == "note":
        return {"markdown": ""}
    if block_type == "animation":
        return await _generate_animation(
            chapter=chapter,
            focus=focus,
            material_text=material_text,
            language=language,
            tracker=tracker,
        )
    lang = normalize_language(language)
    prompts = get_prompt_manager()
    system = prompts.render("book", lang, f"blocks.{block_type}.system")
    if not system:
        raise BlockGenerationError(f"提示词缺 blocks.{block_type}.system（book.yaml 没这个键）")
    base_user = _render_user_message(chapter, focus, material_text, graph_seed, block_type, lang)
    hint: str | None = None
    last_error = ""
    for _attempt in range(BLOCK_MAX_ATTEMPTS):
        user = base_user
        if hint is not None:
            user = base_user + "\n\n" + prompts.render("book", lang, "blocks.repair", issues=hint)
        result = await one_shot(
            system=system, user=user, max_tokens=max_tokens_for(block_type), tracker=tracker
        )
        try:
            return _parse_and_validate(block_type, result.text)
        except (BookOutputError, BookError) as exc:
            last_error = str(exc)
            hint = f"- {exc}"
    raise BlockGenerationError(f"{block_type} 块两次都没生成成功：{last_error}")


async def _generate_animation(
    *,
    chapter: Chapter,
    focus: str,
    material_text: str,
    language: str,
    tracker: CostTracker | None,
) -> dict[str, Any]:
    """animation 块：整条 math_animator 流水线（提示词/渲染/修复都归它管）。"""
    try:
        service = get_render_service()
    except RuntimeError as exc:
        raise BlockGenerationError("渲染服务没装配，动画块做不了") from exc
    report = service.available()
    if not report.ok:
        missing = "、".join(report.missing)
        raise BlockGenerationError(f"缺渲染依赖（{missing}），先补上再编译动画块")
    lang = normalize_language(language)
    prompts = get_prompt_manager()
    question_text = prompts.render(
        "book",
        lang,
        "blocks.animation.user",
        chapter_title=chapter.title,
        focus=focus or chapter.title,
        material=_clip(material_text, ANIMATION_MATERIAL_CHARS),
    )
    session_id = f"book-animation-{chapter.key}"
    unified = UnifiedContext(
        session=SessionRef(id=session_id),
        capability="book",
        message=Message.new(session_id=session_id, role="user", content=question_text),
        language=lang,
    )
    # 编译期不挂任何工具：材料已经在问题正文里，检索留着给页聊天（确定性优先）
    mounted = mount_tools(unified, allowlist=())
    bus = StreamBus(turn_id=new_id("turn"))
    try:
        model_config = resolve_model_config()
        client = create_client(
            model_config.model,
            provider_id=model_config.provider_id,
            base_url=model_config.base_url,
            api_key=model_config.api_key,
        )
        outcome = await run_animation(
            unified,
            bus,
            AnimationRequest(
                service=service,
                client=client,
                mc=model_config,
                mounted=mounted,
                base_history=[],
                question={"role": "user", "content": question_text},
                quality="medium",
            ),
        )
    except LLMError as exc:
        # 一个动画块失败不该拖垮整本书：按坏块处理（流水线内部的模型错误）
        raise BlockGenerationError(f"动画生成失败：{exc}") from exc
    if tracker is not None:
        tracker.merge(unified.cost)
    if not outcome.body:
        raise BlockGenerationError(outcome.error or "动画流水线没有产出正文")
    try:
        return validate_block_payload("animation", {"markdown": outcome.body})
    except BookError as exc:
        raise BlockGenerationError(f"动画正文不合规：{exc}") from exc
