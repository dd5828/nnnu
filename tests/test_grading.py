"""判分器纯单测（§7.4）：单选/多选确定性、简答确定性优先 + LLM 兜底、fail-closed。

不碰数据库：LLM 判分器用桩客户端（回复文案自己给），成本落在传入的 tracker 上。
"""

import pytest

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.learning.grading import (
    DETERMINISTIC,
    LLM,
    GradeInputError,
    ShortAnswerGrader,
    feedback_text,
    grade,
    grade_multi,
    grade_short,
    grade_single,
    labels_of,
    normalize_text,
    parse_grader_reply,
)
from nnnu.services.llm.factory import ModelConfig
from nnnu.services.llm.protocol import LLMResponse

MODEL_CONFIG = ModelConfig(
    provider_id="stub",
    model="stub-model",
    base_url=None,
    api_key=None,
    temperature=0.0,
    reasoning_effort=None,
)


class StubClient:
    """LLMClient 的最小替身：把给定文本当一次 complete 的回复。"""

    def __init__(self, text: str, usage: dict | None = None) -> None:
        self.text = text
        self.usage = usage or {}
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return LLMResponse(text=self.text, usage=self.usage)


def test_normalize_text_strips_wrappers_and_case():
    assert normalize_text("  $\\frac{1}{2}$  ") == normalize_text("\\frac{1}{2}")
    assert normalize_text("ABC") == normalize_text("abc")
    assert normalize_text("ＡＢＣ") == "abc"  # 全角转半角
    assert normalize_text("叶绿体。") == normalize_text(" 叶绿体 ")  # 尾标点与空白


def test_labels_of_accepts_mixed_separators():
    assert labels_of("A、C") == {"A", "C"}
    assert labels_of("ac") == {"A", "C"}
    assert labels_of("") == set()


def test_single_normalized_match():
    assert grade_single("b", "B").score == 1.0
    assert grade_single(" B ", "B").correct is True
    result = grade_single("A", "B")
    assert result.correct is False and result.score == 0.0
    assert result.source == DETERMINISTIC


def test_single_rejects_multiple_labels():
    with pytest.raises(GradeInputError):
        grade_single("AB", "A")


def test_choice_rejects_out_of_range_label():
    with pytest.raises(GradeInputError):
        grade_multi("AE", "A", option_count=4)


def test_multi_scoring_four_cases():
    # (命中 − 错选) / 答案个数：全对 1.0；漏选按比例；错选抵消命中；全错 0.0
    assert grade_multi("AC", "AC").score == 1.0
    assert grade_multi("A", "AC").score == pytest.approx(0.5)  # 漏一个 → 1/2
    assert grade_multi("ABC", "AC").score == pytest.approx(0.5)  # 多选一个错的 → (2−1)/2
    assert grade_multi("AB", "AC").score == 0.0  # 一命中一错选恰好抵消
    assert grade_multi("BD", "AC").score == 0.0
    assert grade_multi("A", "AC").correct is False


def test_choice_empty_answer_fails_closed():
    # 空作答不打扰模型，直接按错
    assert grade_single("", "A").feedback_key == "empty"
    assert grade_multi("  ", "AC").feedback_key == "empty"


def test_choice_empty_key_fails_closed():
    assert grade_single("A", "").correct is False
    assert grade_multi("AC", "").score == 0.0


def test_short_exact_and_jaccard():
    assert grade_short("光合作用", "光合作用").score == 1.0
    # 措辞差一点（多一个「的」）不打扰模型：分词 Jaccard 达标直接算对（实测 0.92）
    near = grade_short("光反应在类囊体薄膜上进行", "光反应在类囊体薄膜上进行的")
    assert near.correct is True and near.needs_llm is False
    # 差得多的交给 LLM 判分器
    assert grade_short("植物进行光合作用", "植物进行光合作用的过程").needs_llm is True


def test_short_unmatched_needs_llm():
    result = grade_short("我不知道", "光合作用在叶绿体中进行")
    assert result.needs_llm is True
    assert result.correct is False
    assert result.feedback_key == "unmatched"


def test_short_empty_answer_or_key_fails_closed():
    assert grade_short("", "答案").needs_llm is False
    assert grade_short("我的答案", "").needs_llm is False
    assert grade_short("   ", "答案").score == 0.0


def test_grade_dispatches_by_type():
    assert grade("single", "A", "A").correct is True
    assert grade("multi", "AC", "AC").correct is True
    # 未知题型按简答走，绝不误判成客观题
    assert grade("short", "光合作用", "光合作用").correct is True


def test_feedback_text_renders_from_prompts(repo_prompts):
    text = feedback_text(grade_single("A", "B"), lang="zh", answer_key="B")
    assert "B" in text
    assert text != "wrong"  # 渲染成功，不是回落的键名


def test_parse_grader_reply_ok_and_fallback():
    fallback = grade_short("我不知道", "光合作用")
    parsed = parse_grader_reply(
        '{"score": 0.8, "correct": true, "feedback": "沾边"}', fallback=fallback
    )
    assert parsed.correct is True and parsed.source == LLM
    assert parsed.feedback == "沾边"
    # 坏了回落确定性结果，绝不重判
    assert parse_grader_reply("不是 JSON", fallback=fallback) is fallback
    assert parse_grader_reply('{"score": "高"}', fallback=fallback) is fallback


def test_parse_grader_reply_score_threshold():
    fallback = grade_short("我不知道", "光合作用")
    parsed = parse_grader_reply('{"score": 0.4, "feedback": "不对"}', fallback=fallback)
    assert parsed.correct is False


async def test_short_answer_grader_uses_llm_and_records_cost(repo_prompts):
    client = StubClient(
        '{"score": 1, "correct": true, "feedback": "意思对"}',
        usage={"prompt_tokens": 30, "completion_tokens": 8},
    )
    grader = ShortAnswerGrader(client, MODEL_CONFIG, language="zh")
    tracker = CostTracker()
    result = await grader.grade(
        stem="光合作用的场所",
        key="叶绿体",
        answer="在叶绿体里进行",
        fallback=grade_short("在叶绿体里进行", "叶绿体"),
        tracker=tracker,
    )
    assert result.correct is True and result.source == LLM
    # 提示词真的渲染了：题干与答案键都进了用户消息
    user_message = client.requests[0].messages[-1]["content"]
    assert "叶绿体" in user_message
    assert tracker.summary()["per_model"]["stub-model"]["input_tokens"] == 30


async def test_short_answer_grader_falls_back_when_reply_broken(repo_prompts):
    client = StubClient("我觉得他答得还行")
    grader = ShortAnswerGrader(client, MODEL_CONFIG)
    fallback = grade_short("在叶绿体里进行", "叶绿体")
    result = await grader.grade(
        stem="场所", key="叶绿体", answer="在叶绿体里进行", fallback=fallback
    )
    assert result == fallback
