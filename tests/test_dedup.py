"""题库查重（§7.4 验收「二次生成重复率 < 20%」）：同题命中、异题放行、短题护栏。

分成两种情况：完全一样的题必判重；同一知识点换问法的题不该判重（否则一整套题
互相误杀）；太短的题面（token 数不足）一律不判重，宁可漏杀不可误杀。
"""

import pytest

from nnnu.services.question_bank.dedup import (
    DEFAULT_THRESHOLD,
    MIN_TOKENS,
    comparison_text,
    find_duplicate,
    is_duplicate,
    low_confidence,
    ranked_similar,
    similarity,
    token_count,
)

STEM = "已知函数 $f(x)=x^2+2x$，求它在 $x=1$ 处的导数值。"
OPTIONS = ["3", "4", "5", "6"]


def test_identical_questions_are_duplicates():
    a = comparison_text(STEM, OPTIONS)
    assert is_duplicate(a, a) is True
    assert similarity(a, a) == 1.0


def test_same_stem_with_reordered_options_still_duplicate():
    a = comparison_text(STEM, OPTIONS)
    b = comparison_text(STEM, ["4", "3", "6", "5"])
    assert is_duplicate(a, b) is True


def test_different_questions_are_not_duplicates():
    a = comparison_text(STEM, OPTIONS)
    b = comparison_text(
        "简述光合作用的光反应阶段发生在哪里，产物是什么。", ["叶绿体", "类囊体薄膜"]
    )
    assert is_duplicate(a, b) is False


def test_same_knowledge_point_rewrite_is_not_duplicate():
    # 同一考点的两道不同题（验收要求：重复率 < 20%，不是 0）
    a = comparison_text("求 $y=\\sin(2x)$ 的导数。", ["$2\\cos(2x)$", "$\\cos(2x)$"])
    b = comparison_text("求 $y=\\cos(3x)$ 的导数。", ["$-3\\sin(3x)$", "$3\\sin(3x)$"])
    assert is_duplicate(a, b) is False


def test_short_text_is_never_a_duplicate():
    # token 数不足 MIN_TOKENS 的短题面：护栏放行，防「1+1=?」这类误杀（长度判不了重复）
    assert MIN_TOKENS == 20
    assert is_duplicate("1+1=?", "1+1=?") is False
    assert is_duplicate(comparison_text("简短题干", []), comparison_text("简短题干", [])) is False


def test_find_duplicate_returns_the_match():
    existing = [comparison_text(f"{STEM} 第 {n} 问", OPTIONS) for n in range(3)]
    target = comparison_text(STEM, OPTIONS)
    assert find_duplicate(target, existing) == existing[0]
    assert (
        find_duplicate(
            comparison_text("化学平衡向哪个方向移动，试说明理由与判断依据。", []), existing
        )
        is None
    )


def test_threshold_is_configurable():
    # 同题改一个字：相似度 0.87（实测），0.8 拦住、0.999 放行——阈值确实在起作用
    a = comparison_text("光合作用把光能转变成化学能储存在有机物中", [])
    b = comparison_text("光合作用把光能转化成化学能储存在有机物中", [])
    assert DEFAULT_THRESHOLD == 0.8
    assert 0.8 <= similarity(a, b) < 0.999
    assert is_duplicate(a, b, threshold=0.8) is True
    assert is_duplicate(a, b, threshold=0.999) is False


def test_same_template_variant_similarity_is_above_dup_threshold():
    # 「同模板只换数字」的变式题：长题干里改一个数，相似度实测 0.9565（钉住防漂移）。
    # 这就是采纳路径**绝不**接 is_duplicate 的原因：变式题会被自家查重（0.8）误杀；
    # 相似度只用来给前端出「可能重题」的提示。
    a = comparison_text(
        "已知函数 $f(x)=x^2+2x+1$，求它在 $x=1$ 处的导数与切线方程，并说明理由。",
        ["$4$", "$3$", "$2$", "$1$"],
    )
    b = comparison_text(
        "已知函数 $f(x)=x^2+2x+1$，求它在 $x=2$ 处的导数与切线方程，并说明理由。",
        ["$6$", "$5$", "$4$", "$3$"],
    )
    assert similarity(a, b) == pytest.approx(0.9565, abs=0.005)
    assert is_duplicate(a, b) is True


def test_ranked_similar_orders_filters_and_limits():
    target = comparison_text("函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？", ["2", "4", "8", "16"])
    near = comparison_text("函数 $f(x)=x^2$ 在 $x=3$ 处的导数是多少？", ["3", "6", "9", "12"])
    far = comparison_text("简述光合作用中光反应发生的场所。", [])
    near_score = pytest.approx(0.7308, abs=0.001)
    assert ranked_similar(target, [far, near], min_score=0.15) == [(1, near_score)]
    # 同分按传入顺序（库里新旧顺序）；limit 截断
    assert ranked_similar(target, [near, near], limit=1) == [(0, near_score)]
    # 空目标文本 / 全部低于 min_score 都是空表
    assert ranked_similar("", [near]) == []
    assert ranked_similar(target, [far]) == []


def test_low_confidence_shares_the_short_text_guard():
    assert token_count("1+1=?") == 1
    assert low_confidence("1+1=?") is True
    long_text = comparison_text(STEM, OPTIONS)
    assert token_count(long_text) >= MIN_TOKENS
    assert low_confidence(long_text) is False
