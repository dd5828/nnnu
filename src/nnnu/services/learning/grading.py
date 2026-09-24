"""判分器（§7.4 答题判分；§5 归 `services/learning/`）。

分工：
- 单选 / 多选：确定性判分。多选给部分分，公式 `(命中 − 错选) / 答案个数`（自定，§7.4 未给数）——
  全对 1 分、漏选按比例、错选抵消命中、全错 0 分；
- 简答：先确定性（归一化后精确相等，或分词 Jaccard 达标），不达标才请 LLM 判分器；
- 空作答、空答案键一律判错（fail-closed）——不去打扰模型。

§16.5：本模块自研，上游 `deeptutor/learning` 只作对照阅读，不复制实现。
"""

from __future__ import annotations

import logging
import re
import unicodedata

from pydantic import BaseModel

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.json_reply import parse_json_reply
from nnnu.services.llm.protocol import LLMClient, LLMRequest
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
