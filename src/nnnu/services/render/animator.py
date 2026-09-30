"""数学动画六阶段流水线（§7.8）：概念分析 → 方案设计 → 代码生成 → 代码修复 → 总结 → 输出产物。

math_animator 能力与 visualize 的 manim 支线共用这一条流水线（`run_animation`），
差异只在能力层怎么进场（依赖检查、配置校验）与怎么收尾。

几个刻意的决定：
- **代码修复段 = 渲染 + 最多 3 次修复**（§7.8 原文「最多 3 次」）：首次生成的代码直接
  渲染，失败才让模型修；总渲染尝试 ≤ `MAX_RENDER_ATTEMPTS`（1 + 3）；修复产出抠不出
  代码块就停（拿同一版再渲染必然再失败，白等一轮）；
- **渲染失败不是 error 事件**：正文里给失败说明 + 最后一版代码 + 日志节选，照常
  done——落库=界面所见，用户拿得到可排查的材料；依赖缺失才是能力层的友好 error；
- 渲染日志按节流回吐（`LOG_STATUS_MIN_INTERVAL_S`），不刷爆事件流；完整日志随
  RenderResult 进正文节选；
- 阶段原文（JSON、代码）由 `SilentBus` 吞掉，正文是流水线自己按契约拼的。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nnnu.capabilities._shared import (
    MountedTools,
    SilentBus,
    append_user_text,
    complete_with_cost,
)
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import Stage
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.render.models import RenderMeta, artifact_payload
from nnnu.services.render.service import LogSink, RenderService
from nnnu.services.render.validation import extract_fenced, extract_scene_class, parse_analysis

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

PROMPT = "math_animator"

# 总渲染尝试数 = 首次生成 1 次 + 最多 3 次修复（§7.8「渲染失败自动修代码，最多 3 次」）
MAX_RENDER_ATTEMPTS = 4
# 渲染日志进 status 的节流间隔（真渲染跑起来后每行都发会把事件流刷爆）
LOG_STATUS_MIN_INTERVAL_S = 1.5
LOG_LINE_MAX_CHARS = 160
# 正文里的日志节选：末尾 30 行（失败诊断足够，且不会把正文撑爆）
LOG_EXCERPT_LINES = 30

# 每阶段挂哪些工具（与设置里开关求交）：只有概念分析需要翻资料
ANIMATION_STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "concept_analysis": ("rag", "attachment_search", "web_search", "web_fetch"),
    "concept_design": (),
    "code_generation": (),
    "code_retry": (),
    "summary": (),
    "render_output": (),
}

# 六阶段声明（能力 manifest 直接引用，保证流水线与界面认识同一套阶段）
ANIMATION_STAGES: tuple[Stage, ...] = (
    Stage(
        key="concept_analysis",
        label_i18n="stages.concept_analysis.label",
        max_rounds=4,
        max_tokens=2048,
    ),
    Stage(
        key="concept_design",
        label_i18n="stages.concept_design.label",
        max_rounds=2,
        max_tokens=4096,
    ),
    Stage(
        key="code_generation",
        label_i18n="stages.code_generation.label",
        max_rounds=4,
        max_tokens=8192,
    ),
    Stage(key="code_retry", label_i18n="stages.code_retry.label", max_rounds=2, max_tokens=8192),
    Stage(key="summary", label_i18n="stages.summary.label", max_rounds=2, max_tokens=1024),
    Stage(
        key="render_output", label_i18n="stages.render_output.label", max_rounds=1, max_tokens=1024
    ),
)


@dataclass(slots=True)
class AnimationRequest:
    """一次动画回合的装配件：模型/客户端/挂载工具/用户消息由能力层备好。"""

    service: RenderService
    client: LLMClient
    mc: ModelConfig
    mounted: MountedTools
    base_history: list[dict[str, Any]]
    question: dict[str, Any]
    quality: str = "medium"
    style_hint: str = ""


@dataclass(slots=True)
class AnimationOutcome:
    """流水线产出：body 恒定有值（成功/失败收场都会拼正文）；阶段中断时 body 为空。"""

    ok: bool = False  # 是否拿到了视频产物
    body: str = ""
    summary: str = ""
    error: str = ""
    attempts: int = 0
    meta: RenderMeta | None = None


async def run_animation(
    ctx: UnifiedContext, bus: StreamBus, request: AnimationRequest
) -> AnimationOutcome:
    """跑完整条六阶段流水线（调用方保证渲染服务已装配且依赖可用）。"""
    prompts = get_prompt_manager()
    lang = ctx.language
    common: dict[str, Any] = {
        "language": {"zh": "中文", "en": "English"}.get(lang, lang),
        "tools": request.mounted.tool_lines(),
        "kb_note": (
            prompts.render(PROMPT, lang, "kb_note", kbs=", ".join(request.mounted.kb_names))
            if request.mounted.kb_names
            else ""
        ),
    }
    preamble = prompts.render(PROMPT, lang, "system", **common)
    style_hint = (
        prompts.render(PROMPT, lang, "style_hint", hint=request.style_hint)
        if request.style_hint
        else ""
    )
    title = prompts.render(PROMPT, lang, "body.untitled")

    # ---- 1. 概念分析 ----
    analysis_text = await _loop_stage(
        ctx,
        bus,
        stage=_stage("concept_analysis"),
        request=request,
        preamble=preamble,
        system=prompts.render(PROMPT, lang, "stages.concept_analysis.system", **common),
        task=prompts.render(PROMPT, lang, "stages.concept_analysis.task", style_hint=style_hint),
    )
    if analysis_text is None:
        return AnimationOutcome(error="概念分析阶段未完成")
    analysis = parse_analysis(analysis_text)

    # ---- 2. 方案设计 ----
    design_text = await _plain_stage(
        ctx,
        bus,
        request,
        stage=_stage("concept_design"),
        system=prompts.render(PROMPT, lang, "stages.concept_design.system", **common),
        task=prompts.render(
            PROMPT,
            lang,
            "stages.concept_design.task",
            style_hint=style_hint,
            analysis_json=_compact(analysis_text),
        ),
    )
    design = parse_analysis(design_text)
    title = str(design.get("title") or analysis.get("learning_goal") or "").strip() or title

    # ---- 3. 代码生成 ----
    code_text = await _loop_stage(
        ctx,
        bus,
        stage=_stage("code_generation"),
        request=request,
        preamble=preamble,
        system=prompts.render(PROMPT, lang, "stages.code_generation.system", **common),
        task=prompts.render(
            PROMPT,
            lang,
            "stages.code_generation.task",
            style_hint=style_hint,
            analysis_json=_compact(analysis_text),
            design_json=_compact(design_text),
        ),
    )
    if code_text is None:
        return AnimationOutcome(error="代码生成阶段未完成")
    code = extract_fenced(code_text, ("python", "py")) or ""
    await bus.emit_status(
        stage="code_generation",
        message=prompts.render(PROMPT, lang, "status.code_prepared"),
    )

    # ---- 4. 渲染 + 修复（首次生成 + 最多 3 次修复 = 总尝试 ≤ 4） ----
    result = await _render_with_retries(ctx, bus, request, code=code, prompts=prompts, lang=lang)
    code, meta, log, last_error, attempts = result

    # ---- 5. 总结（拿不到模型总结时用兜底句，不致命） ----
    render_json = json.dumps(
        {
            "status": "ok" if meta is not None else "failed",
            "attempts": attempts,
            "filename": meta.filename if meta is not None else None,
            "error": None if meta is not None else last_error,
        },
        ensure_ascii=False,
    )
    summary_text = await _plain_stage(
        ctx,
        bus,
        request,
        stage=_stage("summary"),
        system=prompts.render(PROMPT, lang, "stages.summary.system", **common),
        task=prompts.render(
            PROMPT,
            lang,
            "stages.summary.task",
            analysis_json=_compact(analysis_text),
            design_json=_compact(design_text),
            render_json=render_json,
        ),
    )
    summary = str(parse_analysis(summary_text).get("summary_text") or "").strip()
    if not summary:
        summary = prompts.render(PROMPT, lang, "stages.summary.fallback", title=title)

    # ---- 6. 输出产物：拼正文（落库=界面所见） ----
    await bus.emit_status(
        stage="render_output",
        message=prompts.render(PROMPT, lang, "stages.render_output.label"),
    )
    body = _assemble_body(
        prompts,
        lang,
        title=title,
        summary=summary,
        code=code,
        log=log,
        meta=meta,
        error=last_error,
        attempts=attempts,
    )
    return AnimationOutcome(
        ok=meta is not None,
        body=body,
        summary=summary,
        error="" if meta is not None else last_error,
        attempts=attempts,
        meta=meta,
    )


# ---- 渲染与修复 ----


async def _render_with_retries(
    ctx: UnifiedContext,
    bus: StreamBus,
    request: AnimationRequest,
    *,
    code: str,
    prompts: Any,
    lang: str,
) -> tuple[str, RenderMeta | None, str, str, int]:
    """渲染循环：返回（最后一版代码，产物，日志，最后错误，尝试次数）。

    「代码里没有场景类」在本地就判掉，不把注定失败的代码丢给渲染进程。
    """
    render_id = request.service.store.new_render_id()
    sink = _log_sink(bus, prompts, lang)
    attempts = 0
    meta: RenderMeta | None = None
    log = ""
    last_error = ""
    while True:
        attempts += 1
        await bus.emit_status(
            stage="code_retry",
            message=prompts.render(
                PROMPT,
                lang,
                "status.rendering",
                attempt=attempts,
                total=MAX_RENDER_ATTEMPTS,
                quality=request.quality,
            ),
        )
        if not code or extract_scene_class(code) is None:
            ok = False
            log = ""
            last_error = "代码里没有找到可渲染的场景类（继承 Scene / ThreeDScene 的类）"
        else:
            result = await request.service.render(
                render_id,
                code=code,
                quality=request.quality,
                attempts=attempts,
                on_log=sink,
            )
            ok = result.ok
            meta = result.meta
            log = result.log
            last_error = result.error
        if ok:
            await bus.emit_status(
                stage="code_retry",
                message=prompts.render(PROMPT, lang, "status.artifact_ready", attempts=attempts),
            )
            break
        if attempts >= MAX_RENDER_ATTEMPTS:
            logger.warning("数学动画连续 %d 次渲染失败：%s", attempts, last_error)
            break
        await bus.emit_status(
            stage="code_retry",
            message=prompts.render(
                PROMPT, lang, "status.retry", attempt=attempts, error=_short(last_error)
            ),
        )
        repaired_text = await _plain_stage(
            ctx,
            bus,
            request,
            stage=_stage("code_retry"),
            system=prompts.render(PROMPT, lang, "stages.code_retry.system"),
            task=prompts.render(
                PROMPT,
                lang,
                "stages.code_retry.task",
                attempt=attempts,
                error=last_error,
                code=code,
            ),
        )
        repaired = extract_fenced(repaired_text, ("python", "py")) or ""
        if not repaired:
            logger.warning("修复阶段没产出代码块，停止重试")
            break
        code = repaired
    return code, meta, log, last_error, attempts


def _log_sink(bus: StreamBus, prompts: Any, lang: str) -> LogSink:
    """渲染日志 → status 的节流回调（每行都发会把事件流刷爆）。"""
    state = {"last": 0.0}

    async def sink(line: str) -> None:
        now = time.monotonic()
        if now - state["last"] < LOG_STATUS_MIN_INTERVAL_S:
            return
        text = _short(line, LOG_LINE_MAX_CHARS)
        if not text:
            return
        state["last"] = now
        await bus.emit_status(
            stage="code_retry",
            message=prompts.render(PROMPT, lang, "status.log_line", line=text),
        )

    return sink


# ---- 阶段执行 ----


async def _loop_stage(
    ctx: UnifiedContext,
    bus: StreamBus,
    *,
    stage: Stage,
    request: AnimationRequest,
    preamble: str,
    system: str,
    task: str,
) -> str | None:
    """跑一个带工具的阶段循环（正文被 SilentBus 吞掉）；未正常完成返回 None。"""
    prompts = get_prompt_manager()
    await bus.emit_status(
        stage=stage.key, message=prompts.render(PROMPT, ctx.language, stage.label_i18n)
    )
    history = [
        {"role": "system", "content": f"{preamble}\n\n{system}"},
        *request.base_history,
        append_user_text(
            request.question,
            prompts.render(PROMPT, ctx.language, "stage_input.first", stage_task=task),
        ),
    ]
    outcome = await run_agent_loop(
        ctx,
        SilentBus(bus),  # type: ignore[arg-type]
        _deps(
            ctx,
            stage,
            request.mounted.filtered(ANIMATION_STAGE_TOOLS[stage.key]),
            request.client,
            request.mc,
        ),
        history,
    )
    if not outcome.completed:
        logger.warning("math_animator 阶段 %s 未正常完成，就此收尾", stage.key)
        return None
    return outcome.final_text or ""


async def _plain_stage(
    ctx: UnifiedContext,
    bus: StreamBus,
    request: AnimationRequest,
    *,
    stage: Stage,
    system: str,
    task: str,
) -> str:
    """阶段内部的非流式调用（方案设计、修复、总结）：不挂工具，用量并入本回合成本。"""
    prompts = get_prompt_manager()
    await bus.emit_status(
        stage=stage.key, message=prompts.render(PROMPT, ctx.language, stage.label_i18n)
    )
    return await complete_with_cost(
        ctx,
        request.client,
        request.mc,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ],
        temperature=request.mc.temperature,
        max_tokens=stage.max_tokens,
    )


def _deps(
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


def _stage(key: str) -> Stage:
    return next(stage for stage in ANIMATION_STAGES if stage.key == key)


# ---- 正文与文案 ----


def _assemble_body(
    prompts: Any,
    lang: str,
    *,
    title: str,
    summary: str,
    code: str,
    log: str,
    meta: RenderMeta | None,
    error: str,
    attempts: int,
) -> str:
    """正文契约：标题 + 总结 +（产物围栏 或 失败说明）+ python 源码 + 日志节选。"""
    parts = [prompts.render(PROMPT, lang, "body.heading", title=title), summary]
    if meta is not None:
        payload = json.dumps(artifact_payload(meta), ensure_ascii=False, indent=2)
        parts.append(prompts.render(PROMPT, lang, "body.artifact", payload=payload))
    else:
        parts.append(
            prompts.render(
                PROMPT,
                lang,
                "body.failed_note",
                attempts=attempts,
                error=error or "未知错误",
            )
        )
    parts.append(prompts.render(PROMPT, lang, "body.source_heading"))
    parts.append(f"```python\n{code}\n```")
    parts.append(prompts.render(PROMPT, lang, "body.log_heading"))
    excerpt = "\n".join(log.splitlines()[-LOG_EXCERPT_LINES:]).strip()
    parts.append(f"```text\n{excerpt or prompts.render(PROMPT, lang, 'body.no_log')}\n```")
    return "\n\n".join(parts)


def _compact(text: str) -> str:
    """把阶段原文压成单行塞进下一段提示词（JSON 里换行只是浪费 token）。"""
    return " ".join(text.split())


def _short(text: str, limit: int = 200) -> str:
    """日志/错误进 status 与提示词前的清洗：合并空白 + 截断。"""
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[: limit - 1]}…"
