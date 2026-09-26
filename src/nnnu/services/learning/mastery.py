"""节点掌握度与四态判定（§7.5；公式均为自定，集中在 STAGE_LOG 自定数值表）。

掌握度对的是**作答序列**（该节点每一次作答的分数，0–1，按时间升序），不是题目掌握度
的均值：尾部取 ≤5 次，按 `RECENCY_WEIGHTS` 加权平均 ×100，再乘**置信封顶**——只答过
1 次最多算 50 分、2 次最多 80 分。两个作用：
- 防「蒙对一题就判定掌握」；
- 「答对新的把旧的错挤出窗口」：错一次后连对 5 次，那次错滑出尾部，掌握度回到 100。

  0.9 的门 × 2 次封顶 80 ⇒ **至少 3 次作答才可能过门**（刻意如此：门要两次以上证据）。

四态是纯函数（顺序即优先级）：没碰过 → 未开始；已过门且到期 → 需复习；已过门 → 已掌握；
其余 → 学习中。全部可由库里那几列重算，所以看板读数不需要任何后台任务。
本模块只用 TYPE_CHECKING 引 models（models 反过来要在计算字段里用本模块，避免成环）。
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from nnnu.services.learning.policy import (
    MASTERY_MAX,
    QUALITATIVE_FAIL_CAP,
    gate_of,
    is_cleared,
)

if TYPE_CHECKING:
    from nnnu.services.learning.models import LearningNode, NodeState

# 尾部 ≤5 次作答的近因权重（旧的轻、新的重）；不足 5 次时取本表的尾部对齐
RECENCY_WEIGHTS: tuple[float, ...] = (0.5, 0.7, 0.85, 0.95, 1.0)
RECENT_ATTEMPTS = len(RECENCY_WEIGHTS)
# 置信封顶：作答次数 → 分数上限比例；≥3 次不封顶（表里没有就是 1.0）
CONFIDENCE_CAPS: dict[int, float] = {1: 0.5, 2: 0.8}
# 「答对」的判定：多选部分分落在 (0,1) 之间，取 1.0 容差比较
CORRECT_AT = 0.999


def node_mastery(scores: Sequence[float]) -> float:
    """节点级掌握度 0–100（scores = 该节点每次作答的分数，0–1，时间升序）。"""
    values = [max(0.0, min(1.0, float(score))) for score in scores][-RECENT_ATTEMPTS:]
    if not values:
        return 0.0
    weights = RECENCY_WEIGHTS[RECENT_ATTEMPTS - len(values) :]
    weighted = sum(weight * value for weight, value in zip(weights, values))
    raw = weighted / sum(weights)
    cap = CONFIDENCE_CAPS.get(len(values), 1.0)
    return round(min(raw, cap) * MASTERY_MAX, 1)


def trailing_streak(scores: Sequence[float]) -> int:
    """尾部连续答对的次数（连对次数）——不新增列，由作答序列顺带算出。"""
    streak = 0
    for value in reversed(list(scores)):
        if float(value) < CORRECT_AT:
            break
        streak += 1
    return streak


def display_mastery(*, node_type: str, assess_passed: bool, evidence: float) -> float:
    """落库/展示用的节点掌握度。

    定量节点就是算出来的分；定性节点过了门记 100（门是布尔的，没有中间分），
    没过则 `min(证据值, 40)`——给它一个能画出环、但明确低于过关的信号。
    """
    if gate_of(node_type) is not None:
        return evidence
    if assess_passed:
        return MASTERY_MAX
    return round(min(evidence, QUALITATIVE_FAIL_CAP), 1)


def is_due(node: "LearningNode", *, now: float) -> bool:
    """已过门且复习到期（没过门的节点不算「到期复习」，它归下一目标的补学分支）。"""
    return is_cleared(node) and node.next_review_at is not None and node.next_review_at <= now


def compute_state(node: "LearningNode", *, now: float) -> "NodeState":
    """四态判定（顺序即优先级，注释见模块头）。"""
    if is_cleared(node):
        return "reviewing" if is_due(node, now=now) else "mastered"
    if node.last_practiced_at is None:
        return "not_started"
    return "learning"
