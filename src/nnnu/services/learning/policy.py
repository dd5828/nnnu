"""学习策略表：四类节点、两套门与复习阶梯（§7.5 只说「不同 type 不同达标线」，数值全部自定）。

集中放这里是为了让「改数值」只有一处，也让掌握度/门控/复习三处算的是同一套标准。

两套门（拍板 #3）：
- **定量门**（memory / procedure）：节点掌握度 0–100 到 90 才算过；
- **定性门**（concept / design）：没有数字线，靠学习者自己讲一遍的布尔评定（`assess`）。

本模块是学习域的最底层：不 import 同域其它模块（免得与 models 绕成环），
需要节点类型时按鸭子类型读字段。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nnnu.services.learning.models import LearningNode

# 定量门（0–100）：节点级掌握度要 ≥ 这个数才算过门
QUANTITATIVE_GATES: dict[str, float] = {"memory": 90.0, "procedure": 90.0}
# 定性门：过不过由 learning_nodes.assess_passed 决定，没有数字线
QUALITATIVE_TYPES: tuple[str, ...] = ("concept", "design")
ALL_TYPES: tuple[str, ...] = ("memory", "procedure", "concept", "design")

# 脏数据兜底选概念：定性 = fail-closed，不会把没学会的节点判成过关
DEFAULT_TYPE = "concept"

# 节点掌握度是 0–100（题级 questions.mastery 仍是 0–1，两级分工见 questions/models.py）
MASTERY_MAX = 100.0
# 定性没过时的展示掌握度上限：写死 40 是为了让看板一眼看出「离过关还差得远」，
# 而不是拿一个没有分母的原始分装作有进度
QUALITATIVE_FAIL_CAP = 40.0

# 复习阶梯（天）：经典 Leitner 形状。首档都 ≥1 天——不学上游 memory 表那样首档 0 天，
# 否则刚过关的节点立刻算「到期」，会打断用户正在学的新内容
LADDERS: dict[str, tuple[int, ...]] = {
    "memory": (1, 3, 7, 14, 30, 60),
    "procedure": (3, 7, 14),
    "concept": (3, 7, 14, 30),
    "design": (14, 28),
}
CONSECUTIVE_JUMP = 2  # 连续答对达到这个次数：复习档跳两档
DAY_SECONDS = 86400.0


def normalize_type(node_type: str) -> str:
    """未知类型归一到默认类型（脏数据不卡死路径）。"""
    return node_type if node_type in ALL_TYPES else DEFAULT_TYPE


def is_quantitative(node_type: str) -> bool:
    return node_type in QUANTITATIVE_GATES


def gate_of(node_type: str) -> float | None:
    """该类型的定量门；定性类型返回 None（门不是一个数字）。"""
    return QUANTITATIVE_GATES.get(node_type)


def gate_kind_of(node_type: str) -> str:
    """门的两套：quantitative（分数线）/ qualitative（布尔评定）。"""
    return "quantitative" if is_quantitative(node_type) else "qualitative"


def ladder_of(node_type: str) -> tuple[int, ...]:
    return LADDERS[normalize_type(node_type)]


def is_cleared(node: "LearningNode") -> bool:
    """节点是否已过门：定量看掌握度，定性看布尔评定（两者都不是 = 没过）。"""
    gate = gate_of(node.node_type)
    if gate is None:
        return bool(node.assess_passed)
    return node.mastery >= gate


def strategy_key(node_type: str) -> str:
    """教学策略键：指向 prompts/{lang}/mastery.yaml 的 strategies.<key>。"""
    return normalize_type(node_type)
