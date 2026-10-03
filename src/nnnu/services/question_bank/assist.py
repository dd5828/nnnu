"""题库 AI 助手（P9 题库增强）：LLM 分类 + 变式题生成。

照 `services/learning/grading.py` 的判分器模式：
- 严格 JSON 提示词（prompts/{lang}/question_classify.yaml、question_variants.yaml）
  + `services/llm/json_reply.parse_json_reply`，再逐字段清洗；
- 结构校验复用 `service.clean_fields`（与手输题目同一把尺子），过不了的结构弃题；
- 成本记进调用方给的 CostTracker（不是回合；合成 turn_id 由路由负责）。

模型走形的兜底原则：能唯一确定就归一（答案整串等于某选项原文、错因给了中文别名），
含糊一律弃题——答案键是题目的命根子，宁缺毋滥；全批洗不出来抛
`LLMOutputInvalidError`（用户点了按钮，得有个说法，不静默返空）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from pydantic import BaseModel, Field

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.json_reply import parse_json_reply
from nnnu.services.llm.protocol import LLMClient, LLMRequest
from nnnu.services.question_bank.models import ERROR_CAUSES, Question
from nnnu.services.question_bank.service import (
    DIFFICULTIES,
    LABELS,
    MAX_ERROR_CAUSES,
    MAX_KNOWLEDGE_POINT_CHARS,
    MAX_TAG_CHARS,
    MAX_TAGS,
    QUESTION_TYPES,
    QuestionBankError,
    clean_fields,
    normalize_labels,
)

logger = logging.getLogger(__name__)

CLASSIFY_MAX_TOKENS = 512
VARIANTS_MAX_TOKENS = 2048

# 错因的中文名（提示词里给模型看；前端展示文案在 locale questions.cause_* 里各有一份）
CAUSE_LABELS_ZH = {
    "concept_unclear": "概念不清",
    "misread": "审题失误",
    "calculation": "计算失误",
    "method_missing": "方法不会",
    "memory_weak": "记忆不牢",
}

_NONE_TEXT = {"zh": "（无）", "en": "(none)"}

# 选项自带的「A. 」「B、」前缀（模型常画蛇添足；界面按位置推导标签，会显得重复）
_OPTION_LABEL_PREFIX = re.compile(r"^[A-Ha-h][.、．)）:：]\s*")


class LLMOutputInvalidError(LLMError):
    """模型输出洗不出可用结构（不是 JSON / 全批坏题）——路由按 502 返回。"""


class ClassifySuggestion(BaseModel):
    """一次分类的结论（写回题目由 service.apply_classification 负责）。"""

    knowledge_point: str = ""
    tags: list[str] = Field(default_factory=list)
    error_causes: list[str] = Field(default_factory=list)
    reason: str = ""  # 给用户看的一句话依据


# ---- 纯函数（单测直接打这些）----


def _norm_key(text: Any) -> str:
    """别名归一：大小写/空格/下划线/连字符都不敏感（"Concept Unclear" == concept_unclear）。"""
    return re.sub(r"[\s_\-]+", "", str(text).strip().casefold())


_CAUSE_LOOKUP: dict[str, str] = {_norm_key(key): key for key in ERROR_CAUSES}
_CAUSE_LOOKUP.update(
    {
        _norm_key(alias): target
        for alias, target in {
            "概念不清": "concept_unclear",
            "概念模糊": "concept_unclear",
            "概念没记清": "concept_unclear",
            "审题失误": "misread",
            "审题不清": "misread",
            "看错题": "misread",
            "读题马虎": "misread",
            "计算失误": "calculation",
            "计算错误": "calculation",
            "粗心": "calculation",
            "算错": "calculation",
            "方法不会": "method_missing",
            "方法缺失": "method_missing",
            "不会做": "method_missing",
            "记忆不牢": "memory_weak",
            "记忆错误": "memory_weak",
            "记错": "memory_weak",
            "忘了": "memory_weak",
        }.items()
    }
)


def normalize_error_cause(value: Any) -> str | None:
    """错因别名 → 枚举键；认不出返回 None（调用方丢弃，不猜）。"""
    return _CAUSE_LOOKUP.get(_norm_key(value))


def _clean_str_list(raw: Any) -> list[str]:
    """模型可能把数组给成单个字符串——两种都收，其余当空。"""
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw]


def parse_classification(text: str) -> ClassifySuggestion | None:
    """模型回复 → 分类建议；不是 JSON 对象返回 None（路由按 502 处理）。

    截断而非报错：知识点超 80 字、标签超 30 字都是模型啰嗦，不是结构错误。
    """
    parsed = parse_json_reply(text)
    if not isinstance(parsed, dict):
        return None
    point = str(parsed.get("knowledge_point") or "").strip()[:MAX_KNOWLEDGE_POINT_CHARS]
    tags: list[str] = []
    for item in _clean_str_list(parsed.get("tags")):
        tag = str(item).strip()[:MAX_TAG_CHARS]
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    causes: list[str] = []
    for item in _clean_str_list(parsed.get("error_causes")):
        cause = normalize_error_cause(item)
        if cause and cause not in causes:
            causes.append(cause)
        if len(causes) >= MAX_ERROR_CAUSES:
            break
    return ClassifySuggestion(
        knowledge_point=point,
        tags=tags,
        error_causes=causes,
        reason=str(parsed.get("reason") or "").strip(),
    )


def parse_variants(text: str) -> list[Any] | None:
    """模型回复 → 变式题草稿列表（未清洗）；结构不对返回 None（路由按 502 处理）。"""
    parsed = parse_json_reply(text)
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("variants")
    if not isinstance(raw, list):
        return None
    return raw


def _clean_draft_options(raw: Any) -> list[str]:
    """选项归一：字符串直接用；对象取 description/text/content 里的文本（模型常见走形）。"""
    items = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
    cleaned: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = ""
            for key in ("description", "text", "content", "option"):
                if item.get(key):
                    text = str(item[key])
                    break
            if not text:
                return []  # 对象里没有可用文本：整题选项不可信，弃题
        else:
            text = str(item)
        text = _OPTION_LABEL_PREFIX.sub("", text.strip())
        if text:
            cleaned.append(text)
    return cleaned


def _resolve_choice_key(raw: str, options: list[str]) -> str:
    """模型给的答案 → 标签串；读不准返回空串（弃题，不猜答案键）。

    只认两种明确写法：合法标签串（"A" / "AC"，大小写不敏感），或与某个选项原文全等。
    """
    text = (raw or "").strip()
    if not text or not options:
        return ""
    letters = text.upper()
    if letters and all(char in LABELS for char in letters):
        try:
            return normalize_labels(letters, len(options))
        except QuestionBankError:
            return ""
    folded = text.casefold()
    for index, option in enumerate(options):
        if folded == str(option).strip().casefold():
            return LABELS[index]
    return ""


def clean_variant_draft(
    raw: Any,
    *,
    default_type: str = "single",
    default_difficulty: str = "medium",
    default_knowledge_point: str = "",
) -> dict[str, Any] | None:
    """洗一道变式题草稿成入库字段；结构过不了 `clean_fields` 返回 None（调用方弃题）。

    知识点取原题的（变式题与原题同知识点，模型不给这项）；标签不继承（用户自定的
    标签未必适用新题，留空让用户自己打）。
    """
    if not isinstance(raw, dict):
        return None
    stem = str(raw.get("stem") or "").strip()
    if not stem:
        return None
    question_type = str(raw.get("type") or default_type).strip().lower()
    if question_type not in QUESTION_TYPES:
        question_type = default_type
    options = _clean_draft_options(raw.get("options"))
    answer_raw = str(raw.get("answer") or "").strip()
    if question_type == "short":
        answer = answer_raw
    else:
        answer = _resolve_choice_key(answer_raw, options)
        if not answer:
            logger.debug("变式题答案读不准，弃题：%r", answer_raw[:40])
            return None
    difficulty = str(raw.get("difficulty") or default_difficulty).strip().lower()
    if difficulty not in DIFFICULTIES:
        difficulty = default_difficulty
    try:
        cleaned = clean_fields(
            stem=stem,
            answer=answer,
            options=options,
            question_type=question_type,
            explanation=str(raw.get("explanation") or "").strip() or None,
            knowledge_point=default_knowledge_point[:MAX_KNOWLEDGE_POINT_CHARS],
            difficulty=difficulty,
        )
    except QuestionBankError:
        return None
    return {
        "stem": cleaned["stem"],
        "options": cleaned["options"],
        "answer": cleaned["answer"],
        "explanation": cleaned["explanation"] or "",
        "type": question_type,
        "difficulty": cleaned["difficulty"],
        "knowledge_point": cleaned["knowledge_point"],
        "tags": [],
    }


# ---- LLM 调用 ----


def _language_name(lang: str) -> str:
    return {"zh": "中文", "en": "English"}.get(lang, lang)


def _format_options(options: Sequence[str], lang: str) -> str:
    if not options:
        return _NONE_TEXT.get(lang, _NONE_TEXT["en"])
    return "\n".join(f"{LABELS[index]}. {option}" for index, option in enumerate(options))


def _none_text(lang: str) -> str:
    return _NONE_TEXT.get(lang, _NONE_TEXT["en"])


def _causes_text(causes: Sequence[str], lang: str) -> str:
    if not causes:
        return _none_text(lang)
    if lang == "zh":
        return "、".join(CAUSE_LABELS_ZH.get(cause, cause) for cause in causes)
    return ", ".join(causes)


def _cause_catalog(lang: str) -> str:
    if lang == "zh":
        return "；".join(f"{key}（{CAUSE_LABELS_ZH[key]}）" for key in ERROR_CAUSES)
    return ", ".join(ERROR_CAUSES)


def _record_usage(
    tracker: CostTracker | None, config: ModelConfig, usage: dict[str, Any] | None
) -> None:
    if tracker is None or not usage:
        return
    tracker.add_usage(
        provider=config.provider_id or "",
        model=config.model,
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
    )


class QuestionClassifier:
    """题目分类器（提示词 prompts/{lang}/question_classify.yaml）。"""

    def __init__(
        self,
        client: LLMClient,
        model_config: ModelConfig,
        *,
        language: str = "zh",
        max_tokens: int = CLASSIFY_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._language = language
        self._max_tokens = max_tokens

    async def classify(
        self, question: Question, *, tracker: CostTracker | None = None
    ) -> ClassifySuggestion:
        lang = self._language
        prompts = get_prompt_manager()
        messages = [
            {
                "role": "system",
                "content": prompts.render(
                    "question_classify",
                    lang,
                    "system",
                    allowed_causes=_cause_catalog(lang),
                    language=_language_name(lang),
                ),
            },
            {
                "role": "user",
                "content": prompts.render(
                    "question_classify",
                    lang,
                    "user",
                    stem=question.stem,
                    options=_format_options(question.options, lang),
                    answer=question.answer,
                    knowledge_point=question.knowledge_point or _none_text(lang),
                    tags="、".join(question.tags) or _none_text(lang),
                    note=question.note or _none_text(lang),
                ),
            },
        ]
        response = await self._client.complete(
            LLMRequest(
                messages=messages,
                model=self._model_config.model,
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        )
        _record_usage(tracker, self._model_config, response.usage)
        suggestion = parse_classification(response.text)
        if suggestion is None:
            raise LLMOutputInvalidError("分类结果不是 JSON，无法解析")
        return suggestion


class VariantGenerator:
    """变式题生成器（提示词 prompts/{lang}/question_variants.yaml）。

    `material` 非空走 grounded 分支（知识库素材出题）；空则纯 AI。
    """

    def __init__(
        self,
        client: LLMClient,
        model_config: ModelConfig,
        *,
        language: str = "zh",
        max_tokens: int = VARIANTS_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._language = language
        self._max_tokens = max_tokens

    async def generate(
        self,
        question: Question,
        *,
        count: int,
        material: Sequence[str] = (),
        tracker: CostTracker | None = None,
    ) -> list[dict[str, Any]]:
        lang = self._language
        prompts = get_prompt_manager()
        messages = [
            {
                "role": "system",
                "content": prompts.render("question_variants", lang, "system"),
            },
            {
                "role": "user",
                "content": prompts.render(
                    "question_variants",
                    lang,
                    "user_grounded" if material else "user_free",
                    stem=question.stem,
                    options=_format_options(question.options, lang),
                    answer=question.answer,
                    knowledge_point=question.knowledge_point or _none_text(lang),
                    error_causes=_causes_text(question.error_causes, lang),
                    count=str(count),
                    material="\n\n---\n\n".join(material),
                ),
            },
        ]
        response = await self._client.complete(
            LLMRequest(
                messages=messages,
                model=self._model_config.model,
                temperature=0.7,  # 出题要有点变化，不必像判分一样钉死
                max_tokens=self._max_tokens,
            )
        )
        _record_usage(tracker, self._model_config, response.usage)
        raw_list = parse_variants(response.text)
        if raw_list is None:
            raise LLMOutputInvalidError("变式题结果不是 JSON，无法解析")
        drafts = [
            draft
            for item in raw_list
            if (
                draft := clean_variant_draft(
                    item,
                    default_type=question.type,
                    default_difficulty=question.difficulty,
                    default_knowledge_point=question.knowledge_point,
                )
            )
        ]
        if not drafts:
            raise LLMOutputInvalidError("变式题结果全部无法解析")
        return drafts[: max(1, count)]


def _client_and_config() -> tuple[LLMClient, ModelConfig]:
    from nnnu.services.llm.factory import create_client, resolve_model_config

    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    return client, model_config


def classifier_from_settings(*, language: str = "zh") -> QuestionClassifier:
    """按设置里的默认模型建一个分类器（题库页走这条：不绑会话）。"""
    client, model_config = _client_and_config()
    return QuestionClassifier(client, model_config, language=language)


def variant_generator_from_settings(*, language: str = "zh") -> VariantGenerator:
    """按设置里的默认模型建一个变式题生成器。"""
    client, model_config = _client_and_config()
    return VariantGenerator(client, model_config, language=language)
