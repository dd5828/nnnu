"""RRF 融合单测（§7.9）：名次融合的数学性质与确定性。"""

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
