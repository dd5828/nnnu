"""RRF 融合单测（§7.9）：名次融合的数学性质与确定性 + 跨库权重。"""

import pytest

from nnnu.services.knowledge.service import (
    _NO_LEXICAL_RANK,
    LEXICAL_BOOST,
    _kb_weights,
    _KbList,
)
from nnnu.services.rag.rrf import RRF_K, rrf_fuse


def test_single_list_keeps_order():
    fused = rrf_fuse([["a", "b", "c"]])
    assert [item for item, _ in fused] == ["a", "b", "c"]


def test_item_in_both_lists_wins():
    fused = rrf_fuse([["a", "b"], ["b", "a"]])
    # 两路名次互相抵消：名次和相同 → 同分，a 先出现（列表序）
    scores = dict(fused)
    assert scores["a"] == scores["b"]
    fused2 = rrf_fuse([["a", "b", "c"], ["b", "a"]])
    scores2 = dict(fused2)
    assert scores2["a"] == scores2["b"]  # 1+2 与 2+1

    fused3 = rrf_fuse([["a", "b", "c"], ["c", "b"]])
    scores3 = dict(fused3)
    assert scores3["b"] > scores3["a"]  # b 在第二路名次更好


def test_duplicate_ids_in_one_list_count_once():
    fused = rrf_fuse([["a", "a", "b"]])
    scores = dict(fused)
    assert scores["a"] == 1 / (RRF_K + 1)
    assert scores["b"] == 1 / (RRF_K + 2)


def test_empty_inputs_are_safe():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []


def test_weights_are_applied():
    fused = rrf_fuse([["a"], ["b"]], weights=[2.0, 1.0])
    scores = dict(fused)
    assert scores["a"] > scores["b"]


def test_scores_are_deterministic():
    assert rrf_fuse([["a", "b"], ["b", "c"]]) == rrf_fuse([["a", "b"], ["b", "c"]])


# ---- 跨库权重（_kb_weights）：相对余弦 × 字面加成 ----


def _kb(top_cosine, best_lexical=_NO_LEXICAL_RANK, signature="stub:topic"):
    return _KbList(ids=["a"], best_lexical=best_lexical, top_cosine=top_cosine, signature=signature)


def test_kb_weights_use_relative_cosine():
    """权重 = 本库最强余弦 / 全库最强余弦：对题的库压过噪声库（真实语料实测可分）。"""
    weights = _kb_weights([_kb(0.70), _kb(0.49), _kb(0.35)])
    assert weights[0] == 1.0
    assert weights[1] == pytest.approx(0.7)
    assert weights[2] == pytest.approx(0.5)


def test_kb_weights_lexical_boost_tilts_weaker_cosine():
    """字面命中但余弦略低的库，压过纯语义的库（查错误码、专有名词靠它）。"""
    literal = _kb(0.50, best_lexical=1)
    fuzzy = _kb(0.60)
    left, right = _kb_weights([literal, fuzzy])
    assert left == pytest.approx(0.5 / 0.6 * LEXICAL_BOOST)
    assert left > right == 1.0


def test_kb_weights_mixed_signatures_fall_back():
    """换过嵌入模型的库之间余弦不可比：退回「只按名次融合」的老行为。"""
    entries = [_kb(0.70, signature="local:a"), _kb(0.20, signature="local:b")]
    assert _kb_weights(entries) == [1.0, 1.0]
    # 空签名（老数据/异常）同样不参与余弦比较
    assert _kb_weights([_kb(0.70, signature=""), _kb(0.20, signature="")]) == [1.0, 1.0]


def test_kb_weights_missing_cosine_falls_back_per_entry():
    """没有向量命中的库拿不到余弦：它自己退回 1.0，不拖累别的库。"""
    weights = _kb_weights([_kb(None), _kb(0.50), _kb(0.25)])
    assert weights == [1.0, 1.0, pytest.approx(0.5)]


def test_kb_weights_orthogonal_library_gets_zero():
    """完全无关的库（余弦 0）权重归零：有结果但排在所有正分之后。"""
    weights = _kb_weights([_kb(0.60), _kb(0.0, best_lexical=3)])
    assert weights[0] == 1.0
    assert weights[1] == 0.0
