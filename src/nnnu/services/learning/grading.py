"""判分器（§7.4 答题判分；§5 归 `services/learning/`）。

分工：
- 单选 / 多选：确定性判分。多选给部分分，公式 `(命中 − 错选) / 答案个数`（自定，§7.4 未给数）——
  全对 1 分、漏选按比例、错选抵消命中、全错 0 分；
- 简答：先确定性（归一化后精确相等，或分词 Jaccard 达标），不达标才请 LLM 判分器；
- 定性门评定（`QualitativeAssessor`）：判「用户自己讲的一段话」过不过，布尔，fail-closed；
- 空作答、空答案键一律判错（fail-closed）——不去打扰模型。

两条判分入口共用同一套口径：题库页路由与聊天里的 `quiz` 工具都走
`grade_with_fallback`（`grade_with_fallback` 不落库，记作答由调用方决定）。

§16.5：本模块自研，上游 `deeptutor/learning` 只作对照阅读，不复制实现。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.json_reply import parse_json_reply
from nnnu.services.llm.protocol import LLMClient, LLMRequest
from nnnu.services.question_bank.models import Question
from nnnu.services.rag.bm25 import tokenize

logger = logging.getLogger(__name__)

DETERMINISTIC = "deterministic"
LLM = "llm"

LABELS = "ABCDEFGH"
SHORT_JACCARD = 0.9  # 简答「换词但意思一样」的下限（自定）
LLM_PASS_SCORE = 0.6  # LLM 判分没给明确的 correct 时，按分数据此判定
GRADER_MAX_TOKENS = 512

# 简答归一化要去掉的包装字符与首尾标点
_WRAPPER_CHARS = ("$", "\\(", "\\)", "\\[", "\\]", "\\!", "\\,", "\\;", "\\ ")
_PUNCT = "。，、；：！？,.;:!?\"'“”‘’（）()【】[]{}《》<>"


class GradeInputError(ValueError):
    """作答本身不合法（单选选了多个、标签越界）——路由按 422 返回。"""


class GradeResult(BaseModel):
    correct: bool
    score: float = 0.0
    source: str = DETERMINISTIC
    needs_llm: bool = False
    feedback_key: str = ""  # 确定性判分的文案键（prompts/*/grading.yaml: feedback.*）
    feedback: str = ""  # 最终展示文案：LLM 判分器直接给文本，确定性判分由调用方渲染


def normalize_text(text: str) -> str:
    """简答归一：全角转半角、小写、去空白、剥 LaTeX 外壳、去首尾标点。"""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    for char in _WRAPPER_CHARS:
        normalized = normalized.replace(char, "")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip(_PUNCT)


def labels_of(text: str) -> set[str]:
    """答案文本 → 选项标签集合（"a, c" / "AC" / "A、C" 都认）。"""
    return {char for char in (text or "").upper() if char.isascii() and char.isalpha()}


def _jaccard(a: str, b: str) -> float:
    tokens_a, tokens_b = set(tokenize(a)), set(tokenize(b))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def grade_single(answer: str, key: str, *, option_count: int = 0) -> GradeResult:
    return _grade_choice("single", answer, key, option_count=option_count)


def grade_multi(answer: str, key: str, *, option_count: int = 0) -> GradeResult:
    return _grade_choice("multi", answer, key, option_count=option_count)


def _grade_choice(question_type: str, answer: str, key: str, *, option_count: int) -> GradeResult:
    expected = labels_of(key)
    selected = labels_of(answer)
    if not expected:
        # 答案键是空的：判对判错都不负责，按错处理（fail-closed）
        return GradeResult(correct=False, score=0.0, feedback_key="wrong")
    if not selected:
        return GradeResult(correct=False, score=0.0, feedback_key="empty")
    valid = set(LABELS[:option_count]) if option_count else set(LABELS)
    invalid = sorted(selected - valid)
    if invalid:
        raise GradeInputError(f"作答标签 {'、'.join(invalid)} 超出选项范围")
    if question_type == "single" and len(selected) != 1:
        raise GradeInputError("单选题只能选一个选项")
    if selected == expected:
        return GradeResult(correct=True, score=1.0, feedback_key="correct")
    if question_type == "single":
        return GradeResult(correct=False, score=0.0, feedback_key="wrong")
    hit = len(selected & expected)
    miss = len(selected - expected)
    score = max(0.0, min(1.0, (hit - miss) / len(expected)))
    return GradeResult(
        correct=False, score=round(score, 4), feedback_key="partial" if score > 0 else "wrong"
    )


def grade_short(answer: str, key: str) -> GradeResult:
    """简答确定性判分；没把握时 needs_llm=True，交调用方请 LLM 判分器。"""
    answer_text = (answer or "").strip()
    if not answer_text:
        return GradeResult(correct=False, score=0.0, feedback_key="empty")
    key_text = (key or "").strip()
    if not key_text:
        return GradeResult(correct=False, score=0.0, feedback_key="wrong")
    if normalize_text(answer_text) == normalize_text(key_text):
        return GradeResult(correct=True, score=1.0, feedback_key="correct")
    if _jaccard(answer_text, key_text) >= SHORT_JACCARD:
        return GradeResult(correct=True, score=1.0, feedback_key="correct")
    return GradeResult(
        correct=False,
        score=0.0,
        needs_llm=True,
        feedback_key="unmatched",
    )


def grade(question_type: str, answer: str, key: str, *, option_count: int = 0) -> GradeResult:
    """按题型分派；题型未知按简答处理（宽松兜底，绝不误判成客观题）。"""
    if question_type == "single":
        return grade_single(answer, key, option_count=option_count)
    if question_type == "multi":
        return grade_multi(answer, key, option_count=option_count)
    return grade_short(answer, key)


def feedback_text(result: GradeResult, *, lang: str, answer_key: str) -> str:
    """确定性判分的展示文案（模板在 prompts/{lang}/grading.yaml: feedback.*）。"""
    text = get_prompt_manager().render(
        "grading",
        lang,
        f"feedback.{result.feedback_key or 'wrong'}",
        answer=answer_key,
        score=f"{result.score:.2f}",
    )
    return text or result.feedback_key


def parse_grader_reply(text: str, *, fallback: GradeResult) -> GradeResult:
    """LLM 判分回复 → GradeResult；解析不出来回落确定性结果。"""
    parsed = parse_json_reply(text)
    if not isinstance(parsed, dict):
        logger.warning("判分器回复不是 JSON，回落确定性判分")
        return fallback
    raw_score = parsed.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float, str)):
        logger.warning("判分器给的 score 不是数字：%r", raw_score)
        return fallback
    try:
        score = max(0.0, min(1.0, float(raw_score)))
    except ValueError:
        logger.warning("判分器给的 score 不是数字：%r", raw_score)
        return fallback
    verdict = parsed.get("correct")
    correct = bool(verdict) if isinstance(verdict, bool) else score >= LLM_PASS_SCORE
    feedback = str(parsed.get("feedback") or "").strip()
    return GradeResult(correct=correct, score=round(score, 4), source=LLM, feedback=feedback)


class ShortAnswerGrader:
    """简答 LLM 判分器（§7.4；提示词 prompts/{lang}/grading.yaml）。

    答案键是权威：模型判的是「作答与答案键是不是一回事」，不是「模型自己会怎么答」。
    成本记进调用方给的 tracker（判分不是回合，见 §6.9 偏离说明）。
    """

    def __init__(
        self,
        client: LLMClient,
        model_config: ModelConfig,
        *,
        language: str = "zh",
        max_tokens: int = GRADER_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._language = language
        self._max_tokens = max_tokens

    async def grade(
        self,
        *,
        stem: str,
        key: str,
        answer: str,
        options: list[str] | None = None,
        fallback: GradeResult | None = None,
        tracker: CostTracker | None = None,
    ) -> GradeResult:
        prompts = get_prompt_manager()
        messages = [
            {
                "role": "system",
                "content": prompts.render(
                    "grading",
                    self._language,
                    "short.system",
                    language=_language_name(self._language),
                ),
            },
            {
                "role": "user",
                "content": prompts.render(
                    "grading",
                    self._language,
                    "short.user",
                    stem=stem,
                    options=_format_options(options),
                    answer_key=key,
                    answer=answer,
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
        if tracker is not None and response.usage:
            tracker.add_usage(
                provider=self._model_config.provider_id or "",
                model=self._model_config.model,
                input_tokens=int(
                    response.usage.get("prompt_tokens") or response.usage.get("input_tokens") or 0
                ),
                output_tokens=int(
                    response.usage.get("completion_tokens")
                    or response.usage.get("output_tokens")
                    or 0
                ),
            )
        return parse_grader_reply(response.text, fallback=fallback or grade_short(answer, key))


class QualitativeAssessment(BaseModel):
    passed: bool = False
    feedback: str = ""


QUALITATIVE_MAX_TOKENS = 512
# 卡片作答「短到可以只看标签」的长度上限（超过就当成一段解释，不靠单字母猜）
SHORT_LABEL_CHARS = 24


def resolve_choice_submission(answer: str, options: Sequence[str]) -> str:
    """把卡片上的答复归一成选项标签（如 "B"）；读不出返回空串。

    卡片点一下就发回标签本身，但用户也可能写「选 B」「答案是 C」或把选项原文抄回来。
    依次试（每一步都要求**唯一**，含糊就不猜）：
    ① 与某个选项原文全等 → 该标签；
    ② 短答复里只提到一个合法标签 → 它；
    ③ 只有唯一一个选项原文被答复包含 → 该标签；
    ④ 只提到一个合法标签（长答复也认）→ 它。
    """
    text = (answer or "").strip()
    if not text:
        return ""
    valid = list(LABELS[: len(options)]) if options else list(LABELS)
    normalized = normalize_text(text)
    for label, option in zip(valid, options):
        if normalized and normalized == normalize_text(str(option)):
            return label
    mentioned = sorted(labels_of(text) & set(valid))
    if len(mentioned) == 1 and len(normalized) <= SHORT_LABEL_CHARS:
        return mentioned[0]
    hits = [
        label
        for label, option in zip(valid, options)
        if normalize_text(str(option)) and normalize_text(str(option)) in normalized
    ]
    if len(hits) == 1:
        return hits[0]
    if len(mentioned) == 1:
        return mentioned[0]
    return ""


async def grade_with_fallback(
    question: Question,
    answer: str,
    *,
    language: str = "zh",
    tracker: CostTracker | None = None,
) -> tuple[GradeResult, str]:
    """判一道题的作答：确定性判分 →（简答没把握时）LLM 兜底，返回（结果，展示文案）。

    `answer` 对客观题必须是**标签串**（卡片走 `resolve_choice_submission` 归一后再进来）：
    题库页由 UI 给标签、聊天卡片由工具归一，两条通路的判分口径因此完全一致。
    LLM 调用失败保留确定性结论（不把 500 甩给答题的人）；**不落库**——记作答、回写掌握度
    由调用方决定（题库页 `record_attempt`，聊天工具走同一条）。
    """
    result = grade(question.type, answer, question.answer, option_count=len(question.options))
    if result.needs_llm:
        try:
            grader = grader_from_settings(language=language)
            result = await grader.grade(
                stem=question.stem,
                key=question.answer,
                answer=answer,
                options=question.options,
                fallback=result,
                tracker=tracker,
            )
        except LLMError as exc:
            logger.warning("简答判分调用失败，保留确定性结论：%s", exc)
    feedback = result.feedback or feedback_text(result, lang=language, answer_key=question.answer)
    return result, feedback


class QualitativeAssessor:
    """定性门评定器（`assess`）：判「学习者自己讲的一段话」讲没讲清楚，**布尔**。

    fail-closed：回复不是 JSON、或没给 passed 字段，一律判**不过**——定性门宁可让用户
    再讲一遍，也不能把没学会的放过去。提示词在 prompts/{lang}/grading.yaml: qualitative.*
    """

    def __init__(
        self,
        client: LLMClient,
        model_config: ModelConfig,
        *,
        language: str = "zh",
        max_tokens: int = QUALITATIVE_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._language = language
        self._max_tokens = max_tokens

    async def assess(
        self,
        *,
        node_title: str,
        description: str = "",
        answer: str,
        tracker: CostTracker | None = None,
    ) -> QualitativeAssessment:
        prompts = get_prompt_manager()
        messages = [
            {
                "role": "system",
                "content": prompts.render(
                    "grading",
                    self._language,
                    "qualitative.system",
                    language=_language_name(self._language),
                ),
            },
            {
                "role": "user",
                "content": prompts.render(
                    "grading",
                    self._language,
                    "qualitative.user",
                    title=node_title,
                    description=description or "（未给说明）",
                    answer=(answer or "").strip(),
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
        if tracker is not None and response.usage:
            tracker.add_usage(
                provider=self._model_config.provider_id or "",
                model=self._model_config.model,
                input_tokens=int(
                    response.usage.get("prompt_tokens") or response.usage.get("input_tokens") or 0
                ),
                output_tokens=int(
                    response.usage.get("completion_tokens")
                    or response.usage.get("output_tokens")
                    or 0
                ),
            )
        parsed = parse_json_reply(response.text)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("passed"), bool):
            logger.warning("定性评定回复读不出结论，按未通过计：%r", response.text[:200])
            return QualitativeAssessment(passed=False, feedback="")
        return QualitativeAssessment(
            passed=bool(parsed["passed"]), feedback=str(parsed.get("feedback") or "").strip()
        )


def assessor_from_settings(*, language: str = "zh") -> QualitativeAssessor:
    """按设置里的默认模型建一个定性评定器（与 grader_from_settings 同形）。"""
    from nnnu.services.llm.factory import create_client, resolve_model_config

    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    return QualitativeAssessor(client, model_config, language=language)


def _format_options(options: list[str] | None) -> str:
    return "\n".join(f"{LABELS[index]}. {option}" for index, option in enumerate(options or []))


def _language_name(lang: str) -> str:
    return {"zh": "中文", "en": "English"}.get(lang, lang)


def grader_from_settings(*, language: str = "zh") -> ShortAnswerGrader:
    """按设置里的默认模型建一个判分器（题库页判分走这条：不绑会话）。"""
    from nnnu.services.llm.factory import create_client, resolve_model_config

    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    return ShortAnswerGrader(client, model_config, language=language)
