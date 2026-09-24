"""题库模型（§8.2 questions + 偏离补列；作答表 §7.4「保留作答与解析」）。"""

import time
from typing import Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

# 题型：单选 / 多选 / 简答（§7.4）
QuestionType = Literal["single", "multi", "short"]

# 掌握度只看最近几次作答（与 service.RECENCY_WEIGHTS 配套）
RECENT_ATTEMPTS = 5


class Question(BaseModel):
    id: str
    stem: str
    options: list[str] = Field(default_factory=list)  # 裸文本，标签 A/B/C/D 由位置推导
    answer: str  # 客观题存标签（多选升序拼接，如 "AC"）；简答存参考答案文本
    explanation: str | None = None
    source: str | None = None  # 题目来源：deep_question | manual
    tags: list[str] = Field(default_factory=list)
    # 题级掌握度 0–1（§7.15 未给公式，自定：见 service._mastery_from）。
    # §7.5 的节点级掌握度是 0–100——批三做 mastery_path 时在此换算，勿混用。
    mastery: float = 0.0
    wrong_count: int = 0
    last_attempt_at: float | None = None
    created_at: float = Field(default_factory=time.time)
    type: QuestionType = "single"
    knowledge_point: str = ""
    difficulty: str = "medium"
    session_id: str | None = None  # 出自哪次会话（出题能力生成时写入）

    @classmethod
    def new(cls, **kwargs) -> "Question":
        return cls(id=new_id("q"), **kwargs)


class QuestionAttempt(BaseModel):
    """一次作答（判分结果一并留在库里，错题回顾要看）。"""

    id: str
    question_id: str
    session_id: str | None = None
    answer: str = ""
    correct: bool = False
    score: float = 0.0  # 0–1；多选部分分、简答按判分器给分
    feedback: str | None = None
    source: str = "auto"  # 判分来源：deterministic | llm
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def new(cls, *, question_id: str, **kwargs) -> "QuestionAttempt":
        return cls(id=new_id("qa"), question_id=question_id, **kwargs)
