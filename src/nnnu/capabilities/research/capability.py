"""deep_research 能力（§7.6）：rephrasing → decomposing → researching → reporting。

**两段式回合**（本能力与其它阶段能力最大的不同）：一次调研跨**两个用户回合**完成。

- 第一回合：`rephrasing`（用 ask_user 澄清 1~3 问，阻塞式）→ `decomposing`（产出子问题
  大纲）→ 大纲渲染进正文 + 建一行 `research_runs`（status=confirming）→ **回合就结束了**。
- 第二回合：用户答复（点确认按钮，或直接打字「确认」）。**确认与否都在这一回合里判**——
  确认就按大纲开查，提修改意见就重跑一次分解（handoff 带旧大纲 + 用户原话）再开查。
  之后 `researching` 按子问题逐个跑 → `reporting` 成稿（长主题分「引言 → 逐节 → 结论」
  多段写，短主题/answer 模式单次写完，见下）→ 收尾。

大纲存库而不是从前端回传：`regenerate`（`turn_runtime.py` 的 regenerate 不带 config）与
断线重连都会把前端手里的 config 弄丢，而第二回合离了大纲就不知道该研究什么。

**每阶段一份新的 LoopDeps**：`token_budget` 会被循环就地扣减，跨阶段复用会把后续阶段
的预算吃完（分段流水线的通用教训）。研究阶段更极端——每个子问题一份，互不牵连。

**子问题失败隔离**：子循环的 `emit_error` 在 `SilentBus`（`capabilities/_shared.py`，
visualize 共用）里降级成 warning。`StreamBus`
对终局事件每 bus 只认一次，一个子问题检索失败就把整个回合的信封吃掉，报告就没处发了。
降级之后「N 个子问题里 M 个没跑完」变成：warning + `partial` 状态 + 报告里一句话说明。

**子问题并发**：互不牵连的循环就真的并发跑（`MAX_PARALLEL_SUBTOPICS` 个在飞），deep 档
6 个子问题不再串着等。任务按大纲顺序创建、按同一顺序收口，正文块（小标题 + 证据行）
看到的仍是 1、2、3…；进度 status 在各自真正起跑（拿到并发槽）时才发。代价是并发期间
子循环的思考流会互相穿插归不了位，子问题段因此连 thinking 一起静默（`silent_thinking`）。

**多段成稿**：报告模式且实际大纲不少于 `STAGED_REPORT_MIN_SUBTOPICS` 时，成稿不再一次
写完，而是「引言 → 逐节 → 结论」各起一次调用，写一段发一段（长主题一次成稿要么撞上
输出上限被拦腰截断，要么被模型压成什么都说了又什么都没说清的摘要）。分节标题与编号由
代码按大纲生成（`## k. 标题`），报告结构与用户确认过的大纲一一对应；各节只拿自己那条
子问题摘要，引言与结论拿全部。每段各自过一次 `book.renumber`——号在正文里首次引用时才
发，逐段发的顺序就是正文顺序，与整篇一次重排等价。节写崩了就退回那条原始摘要（带着
记号，照样能重排），至少不丢证据。

收尾：`content_done(合并全文)` + `cost_summary` + `done(response=合并全文)`。
**done 不带 citations**：来源已经作为「参考资料」写进报告正文了，再喂一份给通用的
「引用来源」面板就是同一批来源显示两遍（会话导出也会跟着重复一遍）。别的能力靠面板
展示引用，研究这条靠正文。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nnnu.capabilities._shared import (
    MountedTools,
    SilentBus,
    append_user_text,
    build_user_message,
    mount_tools,
    session_history,
)
from nnnu.core.agent_loop import LoopDeps, LoopOutcome, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest, Stage
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import ModelConfig, create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.research.citations import CitationBook
from nnnu.services.research.models import (
    DEPTH_SPECS,
    DEPTHS,
    MAX_SUBTOPIC_TITLE_CHARS,
    MODES,
    DepthSpec,
    ResearchRun,
    SubTopic,
    parse_subtopics,
)
from nnnu.services.research.service import (
    ResearchService,
    get_research_service,
    is_confirmation,
)

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

# 每阶段挂哪些工具（与设置里的开关求交，见 mount_tools）：
# 澄清只发卡；分解与成稿必须是「恰好一次调用、纯文本产出」，挂了工具模型就可能跑去查
# （分解段去检索等于把两段式回合的意义抹掉）；研究段才是真正干活的。
# reason/brainstorm/consult_subagent 一律不进白名单：它们自己会发 LLM 调用，步数不可预测。
STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "rephrasing": ("ask_user",),
    "decomposing": (),
    "researching": (
        "web_search",
        "paper_search",
        "web_fetch",
        "rag",
        "attachment_search",
        "code_execution",
    ),
    "reporting": (),
}

# 子问题同时在飞的个数。取 3 是折中：deep 档 6 个子问题正好两批跑完，又不会把上游
# 检索接口（Bing / arXiv）一轮打满触发限流；单用户自托管场景并发再高也换不来更多带宽。
MAX_PARALLEL_SUBTOPICS = 3

# 多段成稿的触发线：报告模式且**实际**大纲不少于这个数，「成稿」才拆成引言 → 逐节 → 结论
# 各起一次调用。少于它（quick 档 2 个子问题）单次成稿更划算：多出的往返与整篇上限比那点
# 结构收益值钱，报告不长的场景也不会被截断。判定看实际大纲而不是档位声明——模型没拆出
# 那么多子问题时，分节成稿本来就没有意义。
STAGED_REPORT_MIN_SUBTOPICS = 3

# 引言与结论的每次输出上限：它们按设计就短（各 2~4 段），不占报告段整篇的额度；
# 分节正文各自拿 spec.report_tokens——被拆开写以后，每节都有整篇级别的上限可用。
REPORT_FRAME_TOKENS = 2048

# 一份证据都没有时报告正文的兜底话术（成稿段失败且子问题也全军覆没时才会走到）
NO_EVIDENCE_TEXT = "(本次调研没有拿到任何证据。)"

# JSON 代码块围栏（模型很爱加）：先剥围栏再找花括号对象
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class _SourceMarker:
    """把算好的引用记号贴到工具结果里，让模型**抄**而不是**数**（§7.6）。

    约定是「nn = 该来源在本子问题里第几次出现」，可工具结果每次调用都从 `[1]` 重新编号，
    跨轮累积要模型心算——真机跑出来第一段 9 条引用只有 3 条对得上（模型写下 `[CIT-1-1]`
    时心里想的是另一篇）。所以号码在这里算好，跟着工具结果一起进模型看到的文本。

    编号顺序必须与 `CitationBook.add_subtopic` 一致（它就是拿 `sources` 去登记的），
    `[CIT-k-nn]` 才解得回正确的那条来源——所以这份列表是唯一的收集处，
    `LoopOutcome.citations` 在这条链上不再使用。
    """

    def __init__(self, index: int, render_hint: Callable[[str], str]) -> None:
        self.index = index
        self._render_hint = render_hint  # marks → 提示整段（走提示词 i18n）
        self.sources: list[dict[str, Any]] = []
        # 并发版由子任务把循环结果挂回来（免得再往外传一个 (marker, outcome) 元组）
        self.outcome: LoopOutcome | None = None

    def annotate(self, output: str, sources: list[dict[str, Any]]) -> str:
        if not sources:
            return output
        marks: list[str] = []
        for source in sources:
            self.sources.append(source)
            title = " ".join(str(source.get("title") or source.get("doc_id") or "").split())
            marks.append(f"[CIT-{self.index}-{len(self.sources)}] {title[:60]}")
        # 提示整段先渲染好再拼（f-string 里塞不下反斜杠：3.11 是运行时下限）
        hint = self._render_hint("\n".join(marks))
        return f"{output}\n\n{hint}"


class DeepResearchCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="deep_research",
        version="1.0.0",
        # 四阶段名是 §6.4 定死的对外契约，一个字都不能改（前端步骤条按它点亮）
        stages=[
            Stage(
                key="rephrasing",
                label_i18n="stages.rephrasing.label",
                max_rounds=3,
                max_tokens=2048,
            ),
            Stage(
                key="decomposing",
                label_i18n="stages.decomposing.label",
                max_rounds=1,
                max_tokens=2048,
            ),
            # 声明值取 standard 档；实际轮数/输出上限按本次档位现取（见 DEPTH_SPECS）
            Stage(
                key="researching",
                label_i18n="stages.researching.label",
                max_rounds=3,
                max_tokens=3072,
            ),
            Stage(
                key="reporting",
                label_i18n="stages.reporting.label",
                max_rounds=1,
                max_tokens=6144,
            ),
        ],
        config_schema={
            "depth": {
                "type": "string",
                "enum": list(DEPTHS),
                "default": "standard",
                "description": "档位：拆几个子问题、每个查几轮",
            },
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "default": "report",
                "description": "report = 分节研究报告；answer = 直接回答",
            },
            "research_action": {
                "type": "string",
                "enum": ["confirm"],
                "description": "前端「就按这个大纲查」按钮带的强信号（不打字也能确认）",
            },
        },
        default_model_role="chat",
    )

    async def run(self, ctx: UnifiedContext, bus: StreamBus) -> None:
        try:
            service = get_research_service()
        except RuntimeError:
            await bus.emit_error(message="调研服务未装配，暂时用不了。", recoverable=False)
            return
        active = await service.active_run(ctx.session.id)
        if active is None:
            await self._first_turn(ctx, bus, service)
            return
        if active.status == "researching" and not self._is_confirming(ctx):
            # 停在 researching 说明上一回合跑一半断了（stop / 异常），**用户早就确认过**：
            # 这条消息是新问题，不是对大纲的答复。接着跑旧主题会把新问题吃进一份他并不
            # 认识的大纲里，所以作废旧行、按新调研重来。
            logger.info("调研 %s 停在 researching，本消息按新调研处理", active.id)
            await self._abandon(service, active.id)
            await self._first_turn(ctx, bus, service)
            return
        await self._second_turn(ctx, bus, service, active)

    @staticmethod
    def _is_confirming(ctx: UnifiedContext) -> bool:
        """这句用户消息算不算「就按这个大纲来」（按钮强信号 + 确认词表，绝不用 LLM 判）。"""
        return is_confirmation(
            str(ctx.config.get("research_action") or ""), ctx.message.content or ""
        )

    # ---------------- 第一回合：澄清 + 分解 + 出大纲 ----------------

    async def _first_turn(
        self, ctx: UnifiedContext, bus: StreamBus, service: ResearchService
    ) -> None:
        depth = str(ctx.config.get("depth") or "standard")
        mode = str(ctx.config.get("mode") or "report")
        if depth not in DEPTHS or mode not in MODES:
            # §9.2：非法取值零 LLM 调用直接打回，不去猜用户想要哪一种
            await bus.emit_error(
                message=f"非法研究参数 depth={depth!r} mode={mode!r}："
                f"档位只支持 {'、'.join(DEPTHS)}，模式只支持 {'、'.join(MODES)}",
                recoverable=False,
            )
            return

        prompts = get_prompt_manager()
        lang = ctx.language
        spec = DEPTH_SPECS[depth]
        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        common = self._common(prompts, lang, ctx, mounted, depth, mode, spec)
        preamble = prompts.render("deep_research", lang, "system", **common)
        client, mc = self._client(ctx)
        base_history = session_history(ctx)
        question = await build_user_message(ctx, bus, mc.provider_id or "", mc.model)

        # ① 澄清（阻塞式 ask_user；模型问不问都行，问完由它自己收尾给出精炼主题）
        await self._status(prompts, bus, lang, "rephrasing")
        clarifying = await self._loop(
            ctx,
            bus,
            client=client,
            mc=mc,
            mounted=mounted,
            messages=self._messages(
                prompts,
                lang,
                preamble,
                "rephrasing",
                base_history,
                question,
                topic=ctx.message.content.strip(),
                subtopics=spec.subtopics,
            ),
            max_rounds=self._stage("rephrasing").max_rounds,
            max_tokens=self._stage("rephrasing").max_tokens,
            allowlist=STAGE_TOOLS["rephrasing"],
        )
        if not clarifying.completed:
            # 澄清段就崩了（模型/网络问题）：循环已经发过 error，收尾走公共出口
            await self._finish(ctx, bus, "")
            return
        refined = clarifying.final_text.strip() or ctx.message.content.strip()

        # ② 分解
        await self._status(prompts, bus, lang, "decomposing")
        decomposition = await self._loop(
            ctx,
            bus,
            client=client,
            mc=mc,
            mounted=mounted,
            messages=self._messages(
                prompts,
                lang,
                preamble,
                "decomposing",
                base_history,
                question,
                topic=refined,
                subtopics=spec.subtopics,
            ),
            max_rounds=self._stage("decomposing").max_rounds,
            max_tokens=self._stage("decomposing").max_tokens,
            allowlist=STAGE_TOOLS["decomposing"],
        )
        if not decomposition.completed:
            await self._finish(ctx, bus, "")
            return
        subtopics = await self._parse_subtopics(
            prompts, lang, bus, decomposition.final_text, refined, spec
        )

        # ③ 大纲落正文 + 落库（第二回合靠这行知道要研究什么）
        outline = self._render_outline(prompts, lang, refined, subtopics)
        await bus.emit_content_delta(text=outline)
        try:
            await service.create_run(
                session_id=ctx.session.id,
                topic=ctx.message.content.strip(),
                refined_topic=refined,
                mode=mode,
                depth=depth,
                subtopics=subtopics,
            )
        except Exception:
            # 库写不进去就别把用户留在一张点不动的确认卡上：转成 partial 由他重问
            logger.exception("调研建行失败 session=%s", ctx.session.id)
            await bus.emit_warning(message="调研大纲没能存下来，请重新提一次研究主题。")
        await self._finish(ctx, bus, outline)

    # ---------------- 第二回合：确认/改稿 → 检索 → 成稿 ----------------

    async def _second_turn(
        self, ctx: UnifiedContext, bus: StreamBus, service: ResearchService, run: ResearchRun
    ) -> None:
        prompts = get_prompt_manager()
        lang = ctx.language
        spec = run.spec
        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        common = self._common(prompts, lang, ctx, mounted, run.depth, run.mode, spec)
        preamble = prompts.render("deep_research", lang, "system", **common)
        client, mc = self._client(ctx)
        base_history = session_history(ctx)
        question = await build_user_message(ctx, bus, mc.provider_id or "", mc.model)
        topic = run.refined_topic or run.topic
        pieces: list[str] = []

        if self._is_confirming(ctx):
            subtopics = run.subtopics
            await service.confirm_run(run.id, subtopics=subtopics, answer_message_id=ctx.message.id)
        else:
            # 提了修改意见：就地重跑一次分解（handoff 带旧大纲），不再问第二次——
            # 来回确认没有尽头，改完直接开查，不满意下一轮再说。
            await self._status(prompts, bus, lang, "decomposing")
            revised = await self._loop(
                ctx,
                bus,
                client=client,
                mc=mc,
                mounted=mounted,
                messages=self._messages(
                    prompts,
                    lang,
                    preamble,
                    "decomposing",
                    base_history,
                    question,
                    topic=topic,
                    subtopics=spec.subtopics,
                    prev_output=self._render_outline(prompts, lang, topic, run.subtopics),
                ),
                max_rounds=1,
                max_tokens=self._stage("decomposing").max_tokens,
                allowlist=STAGE_TOOLS["decomposing"],
            )
            if not revised.completed:
                await self._abandon(service, run.id)
                await self._finish(ctx, bus, "")
                return
            subtopics = await self._parse_subtopics(
                prompts, lang, bus, revised.final_text, topic, spec
            )
            try:
                await service.update_subtopics(run.id, subtopics=subtopics)
                await service.confirm_run(
                    run.id, subtopics=subtopics, answer_message_id=ctx.message.id
                )
            except Exception:
                logger.exception("调研改稿落库失败 run=%s", run.id)
            revised_outline = self._render_outline(prompts, lang, topic, subtopics, revised=True)
            await bus.emit_content_delta(text=revised_outline)
            pieces.append(f"{revised_outline}\n\n")

        # ③ 子问题并发检索：每个子问题一条独立循环，最多 MAX_PARALLEL_SUBTOPICS 条在飞
        await self._status(prompts, bus, lang, "researching")
        heading = f"## {prompts.render('deep_research', lang, 'stages.researching.heading')}\n\n"
        await bus.emit_content_delta(text=heading)
        pieces.append(heading)

        book = CitationBook()
        findings: list[str] = []
        materials: dict[int, str] = {}  # 子问题序号 → 该子问题的摘要（多段成稿按节取用）
        failed: list[str] = []
        total = len(subtopics)
        slots = asyncio.Semaphore(MAX_PARALLEL_SUBTOPICS)

        async def _run_subtopic(index: int, subtopic: SubTopic) -> _SourceMarker:
            """一个子问题的检索循环。返回值是 marker（来源按它的计数收，见 _SourceMarker）。

            并发期间思考流会互相穿插（thinking 是逐 chunk 拼的），这段连 thinking 一起静默；
            正文块也不在这里发——渲染和进度由主循环按大纲顺序做。
            异常照旧上抛：LLMError 由 soften_errors 在循环内降级，编程错误/脚本耗尽就该炸。
            """
            marker = _SourceMarker(
                index,
                lambda marks: prompts.render(
                    "deep_research", lang, "stages.researching.marker_hint", marks=marks
                ),
            )
            async with slots:  # 排队等槽；等到的顺序即起跑顺序，进度条不会倒着走
                await bus.emit_status(
                    stage="researching",
                    message=prompts.render(
                        "deep_research",
                        lang,
                        "stages.researching.progress_status",
                        index=index,
                        total=total,
                        title=subtopic.title,
                    ),
                )
                outcome = await self._loop(
                    ctx,
                    bus,
                    client=client,
                    mc=mc,
                    mounted=mounted,
                    messages=self._messages(
                        prompts,
                        lang,
                        preamble,
                        "researching",
                        base_history,
                        question,
                        topic=topic,
                        subtopics=spec.subtopics,
                        index=index,
                        total=total,
                        title=subtopic.title,
                        overview=subtopic.overview,
                        k=index,
                    ),
                    max_rounds=spec.tool_rounds,
                    max_tokens=self._stage("researching").max_tokens,
                    allowlist=STAGE_TOOLS["researching"],
                    soften_errors=True,  # 一个子问题失败不许吃掉整个回合的信封
                    silent_thinking=True,  # 并发交错：思考流没法按子问题归位
                    on_tool_result=marker.annotate,
                )
            marker.outcome = outcome
            return marker

        tasks = [
            asyncio.create_task(_run_subtopic(index, subtopic))
            for index, subtopic in enumerate(subtopics, 1)
        ]
        try:
            for index, (task, subtopic) in enumerate(zip(tasks, subtopics, strict=True), 1):
                marker = await task  # 按大纲顺序收口：谁先查完都等前面先交付
                outcome = marker.outcome
                # 用 marker 收的来源而不是 outcome.citations：记号是按 marker 的计数贴的，
                # 两边必须是同一份、同一顺序，后面 `[CIT-k-nn]` 才解得回对应的来源
                book.add_subtopic(index, marker.sources)
                block = prompts.render(
                    "deep_research",
                    lang,
                    "stages.researching.subtopic_heading",
                    index=index,
                    title=subtopic.title,
                )
                await bus.emit_content_delta(text=f"{block}\n\n")
                pieces.append(f"{block}\n\n")
                if outcome is not None and outcome.completed and outcome.final_text.strip():
                    text = outcome.final_text.strip()
                    findings.append(f"### 子问题 {index}：{subtopic.title}\n\n{text}")
                    materials[index] = text
                    line = self._evidence_line(prompts, lang, outcome)
                else:
                    failed.append(subtopic.title)
                    line = prompts.render("deep_research", lang, "stages.researching.failure_line")
                await bus.emit_content_delta(text=f"{line}\n\n")
                pieces.append(f"{line}\n\n")
        finally:
            # 中途异常或用户 stop：兄弟任务一起收掉，别让它们继续花钱、继续发事件
            for task in tasks:
                if not task.done():
                    task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)  # 也顺带领走它们的异常

        # ④ 成稿（多段或单次，+ 逐段引用重排 + 参考资料）
        await self._status(prompts, bus, lang, "reporting")
        report_heading = (
            f"## {prompts.render('deep_research', lang, 'stages.reporting.heading')}\n\n"
        )
        await bus.emit_content_delta(text=report_heading)
        pieces.append(report_heading)
        if failed:
            notice = prompts.render(
                "deep_research",
                lang,
                "stages.reporting.partial_notice",
                total=len(subtopics),
                failed=len(failed),
                titles="、".join(failed),
            )
            await bus.emit_content_delta(text=f"{notice}\n\n")
            pieces.append(f"{notice}\n\n")

        dropped = 0

        async def _emit_report(text: str) -> str:
            """一段成稿正文：重排引用 → 立刻发出去（写一段用户就能看一段）。"""
            nonlocal dropped
            body, count = book.renumber(text.strip())
            dropped += count
            if body:
                await bus.emit_content_delta(text=f"{body}\n\n")
                pieces.append(f"{body}\n\n")
            return body

        if run.mode == "report" and len(subtopics) >= STAGED_REPORT_MIN_SUBTOPICS:
            staged_wrote = await self._staged_report(
                ctx,
                bus,
                prompts,
                lang,
                preamble,
                base_history,
                question,
                client,
                mc,
                mounted,
                run=run,
                spec=spec,
                topic=topic,
                subtopics=subtopics,
                findings=findings,
                materials=materials,
                book=book,
                emit=_emit_report,
            )
            if not staged_wrote:
                await _emit_report(NO_EVIDENCE_TEXT)
        else:
            report = await self._report(
                ctx,
                bus,
                prompts,
                lang,
                preamble,
                base_history,
                question,
                client,
                mc,
                mounted,
                run=run,
                spec=spec,
                findings=findings,
                spec_topic=topic,
            )
            body = await _emit_report(report.final_text) if report else ""
            if not report or not report.completed or not body:
                # 成稿段失败：把已收集的子问题摘要直接交出去，好过交一篇空气
                logger.warning("调研 %s 成稿失败，退回子问题摘要", run.id)
                await _emit_report("\n\n".join(findings) or NO_EVIDENCE_TEXT)
        if dropped:
            await bus.emit_warning(message=f"有 {dropped} 处引用对不上任何来源，已从报告里去掉")
        for line in book.references_lines(
            lambda **kwargs: prompts.render(
                "deep_research", lang, "stages.reporting.references_item", **kwargs
            ),
            heading=prompts.render("deep_research", lang, "stages.reporting.references_heading"),
        ):
            await bus.emit_content_delta(text=f"{line}\n")
            pieces.append(f"{line}\n")

        status = "partial" if failed else "reported"
        try:
            await service.finish_run(run.id, status=status, failed_subtopics=failed)
        except Exception:
            logger.exception("调研收尾落库失败 run=%s", run.id)
        await self._finish(ctx, bus, "".join(pieces))

    # ---------------- 循环与收尾 ----------------

    async def _loop(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        *,
        client: LLMClient,
        mc: ModelConfig,
        mounted: MountedTools,
        messages: list[dict[str, Any]],
        max_rounds: int,
        max_tokens: int,
        allowlist: tuple[str, ...],
        soften_errors: bool = False,
        silent: bool = True,
        silent_thinking: bool = False,
        on_tool_result: Callable[[str, list[dict[str, Any]]], str] | None = None,
    ) -> LoopOutcome:
        """跑一段循环：**每次调用都新建 LoopDeps**（预算会被就地扣减，不能跨段复用）。

        工具按段收窄（`allowlist`）：分解段与成稿段是空集，模型因此没有工具可调，
        只能一次性把文本写出来——这两段的「恰好一次调用」是靠这个保证的，不是靠提示词求它。
        """
        config = ctx.config
        deps = LoopDeps(
            client=client,
            tools=mounted.filtered(allowlist).tool_set(),
            model=mc.model,
            provider=mc.provider_id or "",
            max_rounds=max_rounds,
            max_output_tokens=max_tokens,
            token_budget=config.get("token_budget", 32000),
            temperature=config.get("temperature", mc.temperature),
            reasoning_effort=config.get("reasoning_effort", mc.reasoning_effort),
            thinking_extra=build_reasoning_kwargs(
                provider_id=mc.provider_id or "",
                model=mc.model,
                reasoning_effort=config.get("reasoning_effort"),
            ),
            on_tool_result=on_tool_result,
        )
        return await run_agent_loop(
            ctx,
            SilentBus(  # type: ignore[arg-type]
                bus, silent=silent, silent_thinking=silent_thinking, soften_errors=soften_errors
            ),
            deps,
            messages,
        )

    async def _report(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        prompts: Any,
        lang: str,
        preamble: str,
        base_history: list[dict[str, Any]],
        question: dict[str, Any],
        client: LLMClient,
        mc: ModelConfig,
        mounted: MountedTools,
        *,
        run: ResearchRun,
        spec: DepthSpec,
        findings: list[str],
        spec_topic: str,
    ) -> LoopOutcome:
        """单次成稿：把各子问题的发现一次交进去、一次写完（短报告与 answer 模式走这条）。"""
        system_key = f"stages.reporting.system{'_answer' if run.mode == 'answer' else ''}"
        messages = self._messages(
            prompts,
            lang,
            preamble,
            "reporting",
            base_history,
            question,
            topic=spec_topic,
            subtopics=spec.subtopics,
            system_key=system_key,
            prev_output="\n\n---\n\n".join(findings) or NO_EVIDENCE_TEXT,
        )
        return await self._loop(
            ctx,
            bus,
            client=client,
            mc=mc,
            mounted=mounted,
            messages=messages,
            max_rounds=1,
            max_tokens=spec.report_tokens,
            allowlist=STAGE_TOOLS["reporting"],
        )

    async def _staged_report(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        prompts: Any,
        lang: str,
        preamble: str,
        base_history: list[dict[str, Any]],
        question: dict[str, Any],
        client: LLMClient,
        mc: ModelConfig,
        mounted: MountedTools,
        *,
        run: ResearchRun,
        spec: DepthSpec,
        topic: str,
        subtopics: list[SubTopic],
        findings: list[str],
        materials: dict[int, str],
        book: CitationBook,
        emit: Callable[[str], Awaitable[str]],
    ) -> bool:
        """多段成稿：引言 → 逐节 → 结论，各一次调用、写一段发一段（见模块 docstring）。

        节标题与编号由代码按大纲生成（`## k. 标题`）：大纲是用户确认过的，报告分节必须与它
        一一对上；各节只拿自己那条子问题摘要，引言与结论拿全部（它们要综合）。返回是否写出
        过非空正文——一段都没写出来时调用方还要兜底。节写崩了退回原始摘要（带着引用记号，
        照样能重排），至少不丢证据。
        """
        total = len(subtopics)
        material = "\n\n---\n\n".join(findings) or NO_EVIDENCE_TEXT

        async def _piece(
            piece: str, *, prev_output: str, max_tokens: int, **task_vars: Any
        ) -> LoopOutcome:
            messages = self._messages(
                prompts,
                lang,
                preamble,
                "reporting",
                base_history,
                question,
                prev_output=prev_output,
                system_key=f"stages.reporting.{piece}.system",
                task_key=f"stages.reporting.{piece}.task",
                **task_vars,
            )
            return await self._loop(
                ctx,
                bus,
                client=client,
                mc=mc,
                mounted=mounted,
                messages=messages,
                max_rounds=1,
                max_tokens=max_tokens,
                allowlist=STAGE_TOOLS["reporting"],
            )

        wrote = False
        intro = await _piece(
            "intro", prev_output=material, max_tokens=REPORT_FRAME_TOKENS, topic=topic
        )
        if intro.completed and intro.final_text.strip():
            if await emit(intro.final_text):
                wrote = True

        for index, subtopic in enumerate(subtopics, 1):
            text = materials.get(index)
            if not text:
                continue  # 这个子问题没跑完：partial_notice 已点名，正文里不再给它留空节
            await bus.emit_status(
                stage="reporting",
                message=prompts.render(
                    "deep_research",
                    lang,
                    "stages.reporting.section.progress_status",
                    index=index,
                    total=total,
                    title=subtopic.title,
                ),
            )
            heading = prompts.render(
                "deep_research",
                lang,
                "stages.reporting.section.heading",
                index=index,
                title=subtopic.title,
            )
            section = await _piece(
                "section",
                prev_output=text,
                max_tokens=spec.report_tokens,
                topic=topic,
                index=index,
                total=total,
                title=subtopic.title,
                overview=subtopic.overview,
            )
            body = section.final_text.strip() if section.completed else ""
            if await emit(f"{heading}\n\n{body or text}"):
                wrote = True

        conclusion = await _piece(
            "conclusion", prev_output=material, max_tokens=REPORT_FRAME_TOKENS, topic=topic
        )
        if conclusion.completed and conclusion.final_text.strip():
            if await emit(conclusion.final_text):
                wrote = True
        return wrote

    async def _finish(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        content: str,
    ) -> None:
        """公共收尾（§6.4）：content_done → cost_summary → done。"""
        if content:
            await bus.emit_content_done(full_text=content)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if not bus.terminal_emitted:
            await bus.emit_done(response=content)

    async def _abandon(self, service: ResearchService, run_id: str) -> None:
        try:
            await service.abandon_run(run_id)
        except Exception:
            logger.exception("调研作废失败 run=%s", run_id)

    # ---------------- 提示词装配 ----------------

    @staticmethod
    def _client(ctx: UnifiedContext) -> tuple[LLMClient, ModelConfig]:
        ref = ctx.model
        mc = resolve_model_config(
            override_provider=ref.provider if ref else None,
            override_model=ref.model if ref else None,
        )
        client = create_client(
            mc.model, provider_id=mc.provider_id, base_url=mc.base_url, api_key=mc.api_key
        )
        return client, mc

    @staticmethod
    def _stage(key: str) -> Stage:
        for stage in DeepResearchCapability.manifest.stages:
            if stage.key == key:
                return stage
        raise KeyError(key)  # pragma: no cover - 清单里写死的四个键

    def _common(
        self,
        prompts: Any,
        lang: str,
        ctx: UnifiedContext,
        mounted: MountedTools,
        depth: str,
        mode: str,
        spec: DepthSpec,
    ) -> dict[str, Any]:
        depth_label = prompts.render("deep_research", lang, f"depths.{depth}.label") or depth
        return {
            "language": {"zh": "中文", "en": "English"}.get(lang, lang),
            "tools": mounted.tool_lines(),
            "kb_note": (
                prompts.render("deep_research", lang, "kb_note", kbs=", ".join(mounted.kb_names))
                if mounted.kb_names
                else ""
            ),
            "depth_note": prompts.render(
                "deep_research",
                lang,
                "depth_note",
                depth=depth_label,
                subtopics=spec.subtopics,
                rounds=spec.tool_rounds,
            ),
            "mode_note": prompts.render("deep_research", lang, f"modes.{mode}.note"),
        }

    def _messages(
        self,
        prompts: Any,
        lang: str,
        preamble: str,
        stage: str,
        base_history: list[dict[str, Any]],
        question: dict[str, Any],
        *,
        prev_output: str = "",
        system_key: str | None = None,
        task_key: str | None = None,
        **task_vars: Any,
    ) -> list[dict[str, Any]]:
        """一段循环的完整消息表：系统提示 = 公共正文 + 本段职责；用户消息 = 附件 + 本段任务。"""
        key = system_key or f"stages.{stage}.system"
        stage_system = prompts.render("deep_research", lang, key, **task_vars)
        task = prompts.render(
            "deep_research", lang, task_key or f"stages.{stage}.task", **task_vars
        )
        template = "handoff" if prev_output else "first"
        return [
            {"role": "system", "content": f"{preamble}\n\n{stage_system}"},
            *base_history,
            append_user_text(
                question,
                prompts.render(
                    "deep_research",
                    lang,
                    f"stage_input.{template}",
                    stage_task=task,
                    prev_output=prev_output,
                ),
            ),
        ]

    async def _status(self, prompts: Any, bus: StreamBus, lang: str, stage: str) -> None:
        label = prompts.render("deep_research", lang, f"stages.{stage}.label") or stage
        await bus.emit_status(stage=stage, message=label)

    @staticmethod
    def _evidence_line(prompts: Any, lang: str, outcome: LoopOutcome) -> str:
        """子问题那一行小结：有几条证据就说几条（0 条单独说，别写成「证据 0 条」）。"""
        count = len(outcome.citations)
        key = "stages.researching.evidence_line" if count else "stages.researching.no_evidence_line"
        return prompts.render("deep_research", lang, key, count=count)

    async def _parse_subtopics(
        self,
        prompts: Any,
        lang: str,
        bus: StreamBus,
        raw: str,
        topic: str,
        spec: DepthSpec,
    ) -> list[SubTopic]:
        """解析分解段的 JSON；解析不出来就退回「整题当一个子问题」，绝不让回合空转。

        模型常在 JSON 外面套一层解说或代码块围栏，所以先剥围栏再抓第一个花括号对象。
        两种失败分开发话：JSON 根本没读懂（failed）和读懂了但没有子问题（empty）——
        前者是模型没守协议，后者多半是它认为主题不用拆，排查时不是一回事。
        """
        data = self._extract_json(raw)
        items = parse_subtopics((data or {}).get("sub_topics") if data else None)
        if not items:
            key = "parse.subtopics_empty" if data else "parse.subtopics_failed"
            logger.warning("子问题大纲不可用（%s）：%s", key, (raw or "")[:200])
            await bus.emit_warning(message=prompts.render("deep_research", lang, key))
            return [SubTopic(title=topic[:MAX_SUBTOPIC_TITLE_CHARS], overview="")]
        return items[: spec.subtopics]

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any] | None:
        text = raw or ""
        fenced = _FENCE_RE.search(text)
        if fenced:
            text = fenced.group(1)
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _render_outline(
        prompts: Any, lang: str, topic: str, subtopics: list[SubTopic], *, revised: bool = False
    ) -> str:
        """大纲正文块（第一回合的输出，也是改稿时给模型看的旧大纲）。"""
        key = "outline.revised_heading" if revised else "outline.heading"
        lines = [
            prompts.render("deep_research", lang, key),
            "",
            prompts.render("deep_research", lang, "outline.topic_line", topic=topic),
            "",
        ]
        for index, item in enumerate(subtopics, 1):
            lines.append(
                prompts.render(
                    "deep_research",
                    lang,
                    "outline.item",
                    index=index,
                    title=item.title,
                    overview=item.overview,
                )
            )
        lines += ["", prompts.render("deep_research", lang, "outline.confirm_prompt")]
        return "\n".join(lines)
