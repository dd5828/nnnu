"""学习域模型（§7.5）：知识点树、节点掌握度、四态、复习与看板视图。

两套掌握度别混用：
- **题级** 0–1：一道题最近几次作答的加权平均，题库服务算，存 `questions.mastery`；
- **节点级** 0–100：该节点**作答序列**尾部近因加权 + 置信封顶，本域算，存
  `learning_nodes.mastery`（公式见 mastery.py；定性节点存的是展示值）。

四类节点两套门（拍板 #3）：memory/procedure 定量门（90 分），concept/design 定性门
（布尔评定）。门与是否过门都是 `@computed_field`：不落库、跟着 `model_dump()` 走，
于是 REST 响应与工具 detail 天然同形，前端不用自己再镜像一份门表。
"""

import time
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from nnnu.core.ids import new_id
from nnnu.services.learning.mastery import is_due
from nnnu.services.learning.policy import gate_kind_of, gate_of, is_cleared

# 节点类型（§7.5「每个节点标注 type，不同类型不同达标线」）：四类两套门
NodeType = Literal["memory", "procedure", "concept", "design"]
# 节点四态（§7.5：未开始 / 学习中 / 已掌握 / 需复习）
NodeState = Literal["not_started", "learning", "mastered", "reviewing"]
# 门的两套（`gate_kind` 取值）：quantitative 定量分数线 / qualitative 定性布尔评定
GateKind = Literal["quantitative", "qualitative"]
# 下一目标要做的事（服务端每回合现算，模型没有推进权）
NextAction = Literal["answer_pending", "review", "probe", "practice", "assess", "complete"]

NODE_TYPES: tuple[str, ...] = ("memory", "procedure", "concept", "design")
NODE_STATES: tuple[str, ...] = ("not_started", "learning", "mastered", "reviewing")
NEXT_ACTIONS: tuple[str, ...] = (
    "answer_pending",
    "review",
    "probe",
    "practice",
    "assess",
    "complete",
)


class LearningPath(BaseModel):
    id: str
    topic: str = ""
    title: str = ""
    summary: str | None = None
    # 绑定的学习会话（聊天驱动）。**没有 current_node_id**：门就是游标，
    # 每回合由 next_objective 现算，见 services/learning/service.py
    session_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, **kwargs) -> "LearningPath":
        return cls(id=new_id("lpath"), **kwargs)


class LearningNode(BaseModel):
    id: str
    path_id: str
    parent_id: str | None = None
    title: str
    node_type: NodeType = "concept"
    description: str = ""
    depth: int = 0
    sort_order: int = 0  # 全路径前序连续整数（同路径内唯一有序，前序即学习顺序）
    mastery: float = 0.0  # 节点级 0–100（定量 = 算出来的分；定性 = 展示值）
    state: NodeState = "not_started"
    review_stage: int = 0  # 复习阶梯档位（下标）
    next_review_at: float | None = None
    last_practiced_at: float | None = None
    # 定性门（concept/design）：最近一次评定的结果与时间，可翻转
    assess_passed: bool = False
    assessed_at: float | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, path_id: str, title: str, **kwargs) -> "LearningNode":
        return cls(id=new_id("lnode"), path_id=path_id, title=title, **kwargs)

    # 下面四个是 @computed_field（不落库、跟着 model_dump() 走）：
    # 门/是否过门/是否到期都由服务端算，REST 响应与工具 detail 因此天然同形。
    # mypy 认不出 pydantic 的这套装饰器组合，逐个 `prop-decorator` 忽略。
    @computed_field  # type: ignore[prop-decorator]
    @property
    def gate(self) -> float | None:
        """定量门（分数线）；定性节点为 null（门是布尔的，不是一个数字）。"""
        return gate_of(self.node_type)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gate_kind(self) -> GateKind:
        return gate_kind_of(self.node_type)  # type: ignore[return-value]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cleared(self) -> bool:
        """是否已过门（定量看掌握度、定性看评定）。"""
        return is_cleared(self)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def due(self) -> bool:
        """已过门且复习到期（与 cleared 正交：没过门的节点不会是「到期复习」）。"""
        return is_due(self, now=time.time())


class PathStats(BaseModel):
    """看板汇总。桶按「是否过门」分：mastered + learning + not_started = total。

    `due` 与 mastered **正交**（已过门到期的那批同时计入两者）——统计与进度一律数
    「已过门」，所以进度条不会随时间往回掉。
    """

    total: int = 0
    mastered: int = 0  # 已过门（含到期待复习的）
    learning: int = 0  # 碰过但没过门
    not_started: int = 0
    due: int = 0  # 已过门且到期
    progress: float = 0.0  # 0–1：已过门节点 / 总节点
    weak: int = 0  # 薄弱点个数（作答过但没过门）


class WeakPoint(BaseModel):
    """薄弱点：作答过、仍没过门的节点（看板深链到它的题目）。"""

    node_id: str
    title: str
    node_type: NodeType = "concept"
    mastery: float = 0.0
    gate: float = 0.0  # 定量门；定性记 0（看板按定性样式渲染）
    gate_kind: GateKind = "qualitative"
    gap: float = 0.0  # 距过门还差多少（定量 = 分数线差；定性 = 100 − 展示分）
    attempted: int = 0  # 该节点已作答的题数
    wrong: int = 0  # 该节点错过的次数


class ReviewItem(BaseModel):
    """复习建议：一条到期/在途的复习安排（只有已过门的节点会进来）。"""

    node_id: str
    title: str
    node_type: NodeType = "concept"
    mastery: float = 0.0
    gate: float | None = None
    gate_kind: GateKind = "qualitative"
    state: NodeState = "learning"
    review_stage: int = 0
    next_review_at: float | None = None
    overdue: bool = False  # 已到期（含过期）


class NextTarget(BaseModel):
    """下一目标：服务端由「哪些节点已过门」现算出来的一句话。

    模型读它、照着做；它没有推进权（没有 advance 这种动作），过不过门只看这个对象。
    """

    action: NextAction = "complete"
    node_id: str | None = None
    node_title: str | None = None
    node_type: NodeType | None = None
    gate: float | None = None
    gate_kind: GateKind | None = None
    mastery: float = 0.0
    reason: str = ""  # 人话一句，直接进状态块与工具输出
    due_at: float | None = None

    @property
    def done(self) -> bool:
        return self.action == "complete"


class PathSummary(BaseModel):
    """路径卡片（列表用）：进度 + 已过门数 + 弱点数 + 下一目标。"""

    path: LearningPath
    stats: PathStats
    next_review_at: float | None = None
    next_title: str | None = None  # 下一目标的节点标题
    next_action: str | None = None  # 下一目标要做什么（answers 见 NEXT_ACTIONS）


class PathDetail(BaseModel):
    """路径详情（看板全部数据并入这一个响应，§9.1 不新增看板端点）。"""

    path: LearningPath
    nodes: list[LearningNode] = Field(default_factory=list)  # 前序排列（前端按 depth 缩进）
    stats: PathStats = Field(default_factory=PathStats)
    weak_points: list[WeakPoint] = Field(default_factory=list)
    reviews: list[ReviewItem] = Field(default_factory=list)
    next_target: NextTarget = Field(default_factory=NextTarget)

    def node(self, node_id: str | None) -> LearningNode | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None


class LearningInteraction(BaseModel):
    """一次答题交互（一「回合内出卡—作答—判分」的完整记录）。

    存在的两个理由：
    - 判分来源可追溯（哪道题、答了什么、判了多少、谁判的）；
    - **同一条路径同一时刻只允许一张卡在飞**（部分唯一索引 `status='awaiting_input'`），
      于是「上一题还没答」这件事是库里的状态，不是模型记性好不好。
    """

    id: str
    path_id: str
    node_id: str
    question_id: str = ""
    kind: str = "quiz"  # quiz | probe | assess | grade
    status: str = "awaiting_input"  # awaiting_input | graded | abandoned
    card_prompt: str = ""  # 出卡时的题干（题后来被改/删了也能看懂历史）
    user_answer: str = ""
    correct: bool | None = None
    score: float | None = None
    feedback: str = ""
    grade_source: str = ""  # deterministic | llm | assessor
    session_id: str | None = None
    turn_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    answered_at: float | None = None
    updated_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, path_id: str, node_id: str, **kwargs) -> "LearningInteraction":
        return cls(id=new_id("lint"), path_id=path_id, node_id=node_id, **kwargs)
