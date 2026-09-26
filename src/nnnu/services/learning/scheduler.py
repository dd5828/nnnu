"""复习调度（§7.5「复习建议」；阶梯与升降档规则均为自定）。

判分给的是 0–1 的 score，三种走向：
- `score < 0.5`（这次没答对）：档位退一档（不低于 0）且 `next_review_at = now`——
  立刻进复习队列，这就是「答错后下一轮给针对性讲解」的触发点；
- 没过门：档位不动，下次固定按**首档间隔**排（没过门时谈阶梯没有意义）；
- 过了门：连续答对 ≥2 次进 2 档、否则进 1 档，档位封顶在梯子末尾。

间隔一律从**这次作答**算起，不叠加旧的到期时间；每次正确最多升两档，复习量可预期。
"""

from nnnu.services.learning.policy import (
    CONSECUTIVE_JUMP,
    DAY_SECONDS,
    ladder_of,
)

DEMOTE_BELOW = 0.5  # 低于此分：退档 + 立刻复习


def schedule_next(
    *,
    node_type: str,
    review_stage: int,
    score: float,
    cleared: bool,
    streak: int,
    now: float,
) -> tuple[int, float]:
    """返回（新档位，下次复习时间）。纯函数，不读库。"""
    ladder = ladder_of(node_type)
    stage = max(0, min(review_stage, len(ladder) - 1))
    if score < DEMOTE_BELOW:
        return max(0, stage - 1), now
    if not cleared:
        return stage, now + ladder[0] * DAY_SECONDS
    step = CONSECUTIVE_JUMP if streak >= CONSECUTIVE_JUMP else 1
    stage = min(stage + step, len(ladder) - 1)
    return stage, now + ladder[stage] * DAY_SECONDS
