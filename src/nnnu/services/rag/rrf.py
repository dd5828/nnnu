"""RRF（Reciprocal Rank Fusion）融合（§7.9 混合检索）。

只吃名次、不看分数：向量余弦与 BM25 分数尺度不同、跨 KB 更不可比，
名次是唯一天然可比的量。score(d) = Σ_i 1 / (k + rank_i(d))，k 默认 60
（与参考仓库 llama-index QueryFusionRetriever 的 RECIPROCAL_RANK 一致）。
"""

from __future__ import annotations

from collections.abc import Sequence

RRF_K = 60


def rrf_fuse(
    ranked_lists: Sequence[Sequence[str]],
    *,
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """多路有序 id 列表融合 → 按分数降序的 (id, score)。

    同一路内重复 id 只计首次名次；同分时按首次出现顺序稳定排序
    （保证检索结果可复现，测试可断言）。
    """
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for index, ranked in enumerate(ranked_lists):
        weight = weights[index] if weights else 1.0
        seen_in_list: set[str] = set()
        rank = 0
        for item in ranked:
            if item in seen_in_list:
                continue  # 同一路里重复的 id 只按首次出现计名次，不占名次位
            seen_in_list.add(item)
            rank += 1
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
            if item not in first_seen:
                first_seen[item] = order
                order += 1
    return sorted(scores.items(), key=lambda pair: (-pair[1], first_seen[pair[0]]))
