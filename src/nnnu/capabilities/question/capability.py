"""deep_question 能力（§7.4）：ideation → generation 两阶段 + 一次自评审校。

与 deep_solve 的三点不同：
- generation 段的正文**不发给用户**（模型那段产出是 JSON，中间态给人看没意义）：
  `_SilentContentBus` 把 content_delta / content_done 都吞掉，正文等解析、查重、入库
  之后由能力自己渲染成 markdown 补发——界面上看到的与库里存的是同一份（§7.3 的教训）；
- 两段之后还有一次**非流式**的「自评 + 修正」调用（§7.4 的题目质量自检），
  与 JSON 修复合并成同一次调用，因此**每回合固定 3 次 LLM 调用**；
- 生成即入库（偏离 §7.4 字面的「错题一键入题库」，理由见 STAGE_LOG 偏离清单）：
  查重通过的直接写题库，「错题」= 库里 wrong_count > 0 的那些，题库页按此筛选。

阶段提示词与渲染文案都在 prompts/{lang}/deep_question.yaml；本模块只管流程与校验。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nnnu.capabilities._shared import (
    MountedTools,
    append_user_text,
    build_user_message,
    complete_with_cost,
    mount_tools,
    session_history,
)
from nnnu.core.agent_loop import LoopDeps, run_agent_loop
from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest, Stage
from nnnu.core.events import StreamEvent
from nnnu.core.stream_bus import StreamBus
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import ModelConfig, create_client, resolve_model_config
from nnnu.services.llm.json_reply import json_candidates, parse_json_reply
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.question_bank.dedup import comparison_text, find_duplicate
from nnnu.services.question_bank.models import Question
from nnnu.services.question_bank.service import (
    DIFFICULTIES,
    LABELS,
    QuestionBankError,
    get_question_bank,
)

if TYPE_CHECKING:
    from nnnu.core.context import UnifiedContext

logger = logging.getLogger(__name__)

MIN_QUESTIONS = 1
MAX_QUESTIONS = 10
DEFAULT_QUESTIONS = 5
QUESTION_TYPES = ("single", "multi", "short")
DIFFICULTY_CHOICES = (*DIFFICULTIES, "mixed")
REVIEW_TEMPERATURE = 0.2  # 审校要稳，别自由发挥
REVIEW_MAX_TOKENS = 4096
REVIEW_PAYLOAD_MAX_CHARS = 12000  # 审校轮喂回去的原始输出上限

# 各阶段挂哪些工具（与设置里的开关求交，见 mount_tools）：
# 构思阶段要翻资料（教材/附件/联网/算一算），生成阶段不挂工具——
# 题目是纯写作，挂了只会多烧一轮，而且调用数就没法在测试里数准了。
STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "ideation": ("rag", "attachment_search", "web_search", "code_execution", "ask_user"),
    "generation": (),
}


class _SilentContentBus:
    """只转发状态类事件、把正文吞掉的总线（generation 段专用）。

    循环里有两处 `content_done`（正常收尾 :418 与截断续写兜底 :306），必须都吞——
    漏一处就会把模型的原始 JSON 顶到用户界面上。不继承 StreamBus：继承会多出一份
    `terminal_emitted` / `_history` 状态，收尾判断就分叉了（solve 的 `_StageBus` 同理）。
    """

    def __init__(self, bus: StreamBus) -> None:
        self._bus = bus

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bus, name)

    async def emit_content_delta(self, *, text: str) -> StreamEvent | None:
        return None

    async def emit_content_done(self, *, full_text: str) -> StreamEvent | None:
        return None


class QuestionCapability(BaseCapability):
    manifest = CapabilityManifest(
        name="deep_question",
        version="1.0.0",
        stages=[
            Stage(
                key="ideation", label_i18n="stages.ideation.label", max_rounds=4, max_tokens=2048
            ),
            Stage(
                key="generation",
                label_i18n="stages.generation.label",
                max_rounds=3,
                max_tokens=4096,
            ),
        ],
        config_schema={
            "num_questions": {
                "type": "integer",
                "minimum": MIN_QUESTIONS,
                "maximum": MAX_QUESTIONS,
                "default": DEFAULT_QUESTIONS,
                "description": "题目数量（1–10）",
            },
            "types": {
                "type": "array",
                "items": {"type": "string", "enum": list(QUESTION_TYPES)},
                "default": list(QUESTION_TYPES),
                "description": "题型：single 单选 / multi 多选 / short 简答",
            },
            "difficulty": {
                "type": "string",
                "enum": list(DIFFICULTY_CHOICES),
                "default": "mixed",
                "description": "难度；mixed = 按考点本身的难易分配",
            },
            "knowledge_point": {
                "type": "string",
                "default": "",
                "description": "限定知识点；留空则由模型从用户问题里判断",
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

        mounted = mount_tools(ctx)
        ctx.metadata["rag_kbs"] = mounted.rag_kbs
        spec_text = self._spec_text(prompts, lang, spec)
        preamble = prompts.render(
            "deep_question",
            lang,
            "system",
            language={"zh": "中文", "en": "English"}.get(lang, lang),
            tools=mounted.tool_lines(),
            kb_note=(
                prompts.render("deep_question", lang, "kb_note", kbs=", ".join(mounted.kb_names))
                if mounted.kb_names
                else ""
            ),
        )

        model_ref = ctx.model
        mc = resolve_model_config(
            override_provider=model_ref.provider if model_ref else None,
            override_model=model_ref.model if model_ref else None,
        )
        client = create_client(
            mc.model, provider_id=mc.provider_id, base_url=mc.base_url, api_key=mc.api_key
        )
        base_history = session_history(ctx)
        question = await build_user_message(ctx, bus, mc.provider_id, mc.model)

        pieces: list[str] = []
        completed = True
        generation_heading = self._heading(prompts, lang, "generation")

        # ---- 第一段：构思（正常流式，用户看得到）----
        ideation_label = self._label(prompts, lang, "ideation")
        ideation_heading = self._heading(prompts, lang, "ideation")
        await bus.emit_status(stage="ideation", message=ideation_label)
        header = f"## {ideation_heading}\n\n"
        await bus.emit_content_delta(text=header)
        pieces.append(header)
        history = [
            {
                "role": "system",
                "content": f"{preamble}\n\n"
                f"{prompts.render('deep_question', lang, 'stages.ideation.system')}",
            },
            *base_history,
            append_user_text(question, self._stage_input(prompts, lang, "ideation", "", "")),
        ]
        outcome = await run_agent_loop(
            ctx,
            bus,
            self._deps(
                ctx, self.manifest.stages[0], mounted.filtered(STAGE_TOOLS["ideation"]), client, mc
            ),
            history,
        )
        if not outcome.completed:
            logger.warning("deep_question 构思阶段未正常完成，就此收尾")
            completed = False
        else:
            plan_text = outcome.final_text or ""
            pieces.append(plan_text)

        questions: list[dict[str, Any]] = []
        raw_text = ""
        if completed:
            # ---- 第二段：生成（正文吞掉，产出是 JSON）----
            await bus.emit_status(
                stage="generation", message=self._label(prompts, lang, "generation")
            )
            history = [
                {
                    "role": "system",
                    "content": f"{preamble}\n\n"
                    f"{prompts.render('deep_question', lang, 'stages.generation.system', spec=spec_text, schema_note=prompts.render('deep_question', lang, 'schema_note'))}",
                },
                *base_history,
                append_user_text(
                    question,
                    self._stage_input(prompts, lang, "generation", ideation_label, plan_text),
                ),
            ]
            outcome = await run_agent_loop(
                ctx,
                _SilentContentBus(bus),  # type: ignore[arg-type]
                self._deps(
                    ctx,
                    self.manifest.stages[1],
                    mounted.filtered(STAGE_TOOLS["generation"]),
                    client,
                    mc,
                ),
                history,
            )
            completed = outcome.completed
            raw_text = outcome.final_text or ""
            if completed:
                questions, issues = self._parse_questions(raw_text, spec)
                questions = await self._review(
                    ctx,
                    bus,
                    client,
                    mc,
                    prompts,
                    lang,
                    spec,
                    spec_text,
                    raw_text,
                    questions,
                    issues,
                )
                if not questions:
                    await bus.emit_error(
                        message="题目生成失败：模型没有返回可用的题目 JSON", recoverable=False
                    )
                    completed = False

        if completed:
            stored, dropped = await self._store(ctx, questions, spec, bus)
            body = self._render_questions(prompts, lang, stored, dropped)
            pieces.append(f"{'' if not pieces else chr(10) * 2}## {generation_heading}\n\n{body}")

        combined = "".join(pieces)
        if completed:
            await bus.emit_content_done(full_text=combined)
        summary = ctx.cost.summary()
        await bus.emit_cost_summary(
            tokens=summary["tokens"], cost=summary["cost"], per_model=summary["per_model"]
        )
        if completed and not bus.terminal_emitted:
            await bus.emit_done(response=combined)

    # ---- 配置 ----

    @staticmethod
    def _read_config(ctx: UnifiedContext) -> tuple[dict[str, Any], list[str]]:
        """config → 归一后的出题规格 + 问题清单（有问题就整体拒绝，见 §9.2）。"""
        raw = ctx.config
        errors: list[str] = []
        num_questions = DEFAULT_QUESTIONS
        try:
            num_questions = int(raw.get("num_questions", DEFAULT_QUESTIONS))
        except (TypeError, ValueError):
            errors.append(f"题量 {raw.get('num_questions')!r} 不是整数")
        else:
            if not MIN_QUESTIONS <= num_questions <= MAX_QUESTIONS:
                errors.append(f"题量 {num_questions} 超出范围（{MIN_QUESTIONS}–{MAX_QUESTIONS}）")

        types = raw.get("types") or list(QUESTION_TYPES)
        if isinstance(types, str):  # 宽容一点：单个字符串当一项
            types = [types]
        plain_types = [str(item).strip() for item in types]
        unknown_types = [item for item in plain_types if item not in QUESTION_TYPES]
        if unknown_types:
            errors.append(f"未知题型 {'、'.join(unknown_types)}，允许：{'、'.join(QUESTION_TYPES)}")
        elif not plain_types:
            errors.append("题型至少要选一种")

        difficulty = str(raw.get("difficulty") or "mixed").strip()
        if difficulty not in DIFFICULTY_CHOICES:
            errors.append(f"未知难度 {difficulty!r}，允许：{'、'.join(DIFFICULTY_CHOICES)}")

        return (
            {
                "num_questions": num_questions,
                "types": [item for item in plain_types if item in QUESTION_TYPES],
                "difficulty": difficulty,
                "knowledge_point": str(raw.get("knowledge_point") or "").strip(),
            },
            errors,
        )

    @staticmethod
    def _spec_text(prompts: Any, lang: str, spec: dict[str, Any]) -> str:
        """给模型的规格文本：题量/题型/难度/知识点，标签走提示词 YAML（双语只维护一份）。"""
        type_labels = [
            prompts.render("deep_question", lang, f"type_labels.{name}") or name
            for name in spec["types"]
        ]
        lines = [
            prompts.render("deep_question", lang, "spec.intro"),
            prompts.render("deep_question", lang, "spec.count", count=spec["num_questions"]),
            prompts.render("deep_question", lang, "spec.types", types="、".join(type_labels)),
            prompts.render(
                "deep_question",
                lang,
                "spec.difficulty",
                difficulty=prompts.render(
                    "deep_question", lang, f"difficulty_labels.{spec['difficulty']}"
                )
                or spec["difficulty"],
            ),
            (
                prompts.render(
                    "deep_question",
                    lang,
                    "spec.knowledge_point",
                    knowledge_point=spec["knowledge_point"],
                )
                if spec["knowledge_point"]
                else prompts.render("deep_question", lang, "spec.knowledge_point_any")
            ),
        ]
        return "\n".join(f"- {line}" if index else line for index, line in enumerate(lines))

    # ---- 解析与校验 ----

    def _parse_questions(
        self, text: str, spec: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """模型输出 → 可入库的题目 + 问题清单（问题清单喂给审校轮去修）。"""
        items = self._question_items(parse_json_reply(text))
        if items is None:
            return [], ["上一次输出没能解析成 JSON 题目数组"]
        questions: list[dict[str, Any]] = []
        issues: list[str] = []
        for index, item in enumerate(items, start=1):
            cleaned, problems = self._clean_question(item, index, spec)
            issues.extend(problems)
            if cleaned is not None:
                questions.append(cleaned)
        return questions, issues

    @staticmethod
    def _question_items(parsed: Any) -> list[Any] | None:
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            inner = parsed.get("questions")
            if isinstance(inner, list):
                return inner
            if "stem" in parsed:  # 只给了一道题的形状，也认
                return [parsed]
        return None

    @staticmethod
    def _clean_question(
        item: Any, index: int, spec: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """单题结构校验：能修的修（剥标签前缀、补默认值），修不了的记问题并丢弃。

        与 QuestionBankService.clean_fields 的口径保持一致——那边还会再验一遍，
        这里提前验是为了把问题喂给审校轮，而不是等入库时整体报错。
        """
        if not isinstance(item, dict):
            return None, [f"第 {index} 题不是 JSON 对象"]
        issues: list[str] = []
        stem = str(item.get("stem") or "").strip()
        if not stem:
            issues.append(f"第 {index} 题缺题面")
        question_type = str(item.get("type") or "single").strip().lower()
        if question_type not in QUESTION_TYPES:
            issues.append(f"第 {index} 题题型 {question_type!r} 不认识")
        raw_options = item.get("options")
        options = (
            [str(option).strip() for option in raw_options] if isinstance(raw_options, list) else []
        )
        options = [option for option in options if option]
        options = [_strip_label_prefix(option, position) for position, option in enumerate(options)]
        answer = str(item.get("answer") or "").strip()
        explanation = str(item.get("explanation") or "").strip()
        if not explanation:
            issues.append(f"第 {index} 题没写解析")
        knowledge_point = str(item.get("knowledge_point") or "").strip() or spec["knowledge_point"]
        difficulty = str(item.get("difficulty") or "").strip().lower() or "medium"
        if difficulty not in DIFFICULTIES:
            issues.append(f"第 {index} 题难度 {difficulty!r} 不认识，已按 medium 处理")
            difficulty = "medium"

        if question_type == "short":
            options = []
            if not answer:
                issues.append(f"第 {index} 题（简答）缺参考答案")
        else:
            if len(options) < 2:
                issues.append(f"第 {index} 题选项少于 2 个")
            elif len(set(options)) != len(options):
                issues.append(f"第 {index} 题有重复选项")
            labels = {char for char in answer.upper() if char.isascii() and char.isalpha()}
            if not labels:
                issues.append(f"第 {index} 题（选择题）答案不是选项标签")
            elif not labels <= set(LABELS[: len(options)]):
                issues.append(f"第 {index} 题答案标签超出选项范围（只有 {LABELS[: len(options)]}）")
            elif question_type == "single" and len(labels) != 1:
                issues.append(f"第 {index} 题是单选却给了 {len(labels)} 个答案标签")
            elif question_type == "multi" and len(labels) < 2:
                issues.append(f"第 {index} 题是多选却只给了 {len(labels)} 个答案标签")
            else:
                answer = "".join(sorted(labels))

        if issues:
            # 有问题的题先不丢：审校轮多半能修好，修不好还有生成版兜底
            logger.info("deep_question 第 %d 题结构问题：%s", index, "；".join(issues))
        if not stem or question_type not in QUESTION_TYPES:
            return None, issues
        if question_type == "short" and not answer:
            return None, issues
        if question_type != "short" and (len(options) < 2 or not answer):
            return None, issues
        return (
            {
                "stem": stem,
                "options": options,
                "answer": answer,
                "type": question_type,
                "explanation": explanation,
                "knowledge_point": knowledge_point,
                "difficulty": difficulty,
            },
            issues,
        )

    # ---- 自评审校 ----

    async def _review(
        self,
        ctx: UnifiedContext,
        bus: StreamBus,
        client: LLMClient,
        mc: ModelConfig,
        prompts: Any,
        lang: str,
        spec: dict[str, Any],
        spec_text: str,
        raw_text: str,
        questions: list[dict[str, Any]],
        issues: list[str],
    ) -> list[dict[str, Any]]:
        """§7.4 质量自检：自评 + 修正 + JSON 修复，合并成同一次非流式调用。

        解析得出来就喂 JSON（保住模型原始的转义写法），解析不出来就喂原文让它重整；
        审校版修坏了（解析不出/变空）就回落生成版，宁可少改不可全丢。
        """
        try:
            if questions:
                issue_block = (
                    prompts.render(
                        "deep_question",
                        lang,
                        "self_check.issues_intro",
                        issues="\n".join(f"- {issue}" for issue in issues),
                    )
                    if issues
                    else ""
                )
                user = prompts.render(
                    "deep_question",
                    lang,
                    "self_check.user",
                    spec=spec_text,
                    issues=issue_block,
                    payload=_payload_text(raw_text),
                )
            else:
                user = prompts.render(
                    "deep_question",
                    lang,
                    "self_check.user_broken",
                    spec=spec_text,
                    payload=raw_text[:REVIEW_PAYLOAD_MAX_CHARS],
                )
            text = await complete_with_cost(
                ctx,
                client,
                mc,
                messages=[
                    {
                        "role": "system",
                        "content": prompts.render("deep_question", lang, "self_check.system"),
                    },
                    {"role": "user", "content": user},
                ],
                temperature=REVIEW_TEMPERATURE,
                max_tokens=REVIEW_MAX_TOKENS,
            )
        except LLMError as exc:
            await bus.emit_warning(message=f"题目自检未完成，按生成结果入库：{exc.message}")
            return questions
        reviewed, reviewed_issues = self._parse_questions(text, spec)
        if reviewed_issues:
            logger.info("deep_question 审校后仍有结构问题：%s", "；".join(reviewed_issues))
        if reviewed or not questions:
            return reviewed
        return questions

    # ---- 查重与入库 ----

    async def _store(
        self,
        ctx: UnifiedContext,
        questions: list[dict[str, Any]],
        spec: dict[str, Any],
        bus: StreamBus,
    ) -> tuple[list[Question], int]:
        """查重（库内 + 本批内部）后入库；返回（入库的题，被跳过的重复数）。"""
        service = get_question_bank()
        pool = await service.existing_texts(spec["knowledge_point"] or None)
        accepted: list[dict[str, Any]] = []
        dropped = 0
        for question in questions:
            text = comparison_text(question["stem"], question["options"])
            if find_duplicate(text, pool) is not None:
                dropped += 1
                continue
            pool.append(text)
            accepted.append(question)
        if not accepted:
            return [], dropped
        try:
            stored = await service.add_questions(
                [
                    {**question, "source": "deep_question", "session_id": ctx.session.id}
                    for question in accepted
                ]
            )
        except QuestionBankError as exc:
            await bus.emit_error(message=f"题目入库失败：{exc}", recoverable=False)
            return [], dropped
        return stored, dropped

    # ---- 渲染 ----

    @staticmethod
    def _render_questions(prompts: Any, lang: str, questions: list[Question], dropped: int) -> str:
        """把入库的题渲染成 markdown（不显示答案：作答与判分在题库页做）。"""
        if not questions:
            return prompts.render("deep_question", lang, "render.no_questions")
        blocks: list[str] = []
        for index, question in enumerate(questions, start=1):
            type_label = (
                prompts.render("deep_question", lang, f"type_labels.{question.type}")
                or question.type
            )
            block = prompts.render(
                "deep_question",
                lang,
                "render.question",
                index=index,
                type_label=type_label,
                stem=question.stem,
            )
            for position, option in enumerate(question.options):
                block += "\n" + prompts.render(
                    "deep_question",
                    lang,
                    "render.option",
                    label=LABELS[position],
                    text=option,
                )
            blocks.append(block)
        text = "\n\n".join(blocks)
        if dropped:
            text += "\n\n" + prompts.render(
                "deep_question", lang, "render.duplicates", dropped=dropped
            )
        return text

    # ---- 装配 ----

    @staticmethod
    def _label(prompts: Any, lang: str, key: str) -> str:
        return prompts.render("deep_question", lang, f"stages.{key}.label") or key

    @staticmethod
    def _heading(prompts: Any, lang: str, key: str) -> str:
        return prompts.render("deep_question", lang, f"stages.{key}.heading") or key

    @staticmethod
    def _stage_input(
        prompts: Any, lang: str, key: str, previous_stage: str, previous_text: str
    ) -> str:
        """本阶段接在用户消息后那一段：首段只有任务，后续段带上一阶段的产出。"""
        task = prompts.render("deep_question", lang, f"stages.{key}.task")
        template = "handoff" if previous_text else "first"
        return prompts.render(
            "deep_question",
            lang,
            f"stage_input.{template}",
            stage_task=task,
            prev_stage=previous_stage,
            prev_output=previous_text,
        )

    @staticmethod
    def _deps(
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


def _strip_label_prefix(option: str, position: int) -> str:
    """剥掉模型爱写的选项前缀（"A. 文本" / "A、文本" / "(A) 文本"）。"""
    text = option.strip()
    for prefix in (
        f"{LABELS[position]}.",
        f"{LABELS[position]}、",
        f"{LABELS[position]})",
        f"({LABELS[position]})",
    ):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _payload_text(raw_text: str) -> str:
    """审校轮喂回去的题目文本：能解析就取 JSON 那段（保住原始转义），否则给原文。"""
    if parse_json_reply(raw_text) is not None:
        candidates = json_candidates(raw_text)
        if candidates:
            return candidates[0][:REVIEW_PAYLOAD_MAX_CHARS]
    return raw_text[:REVIEW_PAYLOAD_MAX_CHARS]
