"""深度研究域模型（§7.6）：一次调研的草稿本 + 档位/模式的唯一取值表。

**为什么要有 `research_runs` 这张表**：研究是两段式回合——第一回合只跑到「子问题大纲 +
等确认」就结束，用户答复之后第二回合才检索与成稿，中间隔着一次真实的人机往返。
大纲是第二回合的输入，必须落库（前端回传的 config 在 `regenerate` 与断线重连时会丢，
从 assistant 正文里反向解析又太脆）。

状态机：

    （第一回合建行）──► confirming ──(用户确认)──► researching ──┬─► reported
                          │                                    ├─► partial（有子问题没跑完）
                          └──(stop / 异常 / 超 TTL)──► abandoned  └─► abandoned

只有 `confirming` / `researching` 算「在飞」（占着那条部分唯一索引）；三个终态都不占位，
所以同一个会话可以接着发起下一次调研。
"""

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id

# 调研状态：confirming 等用户确认大纲 / researching 正在检索 / reported 成稿 /
# partial 成稿但有子问题没跑完 / abandoned 被丢弃（stop、异常、超时）
RunStatus = Literal["confirming", "researching", "reported", "partial", "abandoned"]
RUN_STATUSES: tuple[str, ...] = (
    "confirming",
    "researching",
    "reported",
    "partial",
    "abandoned",
)
ACTIVE_STATUSES: tuple[str, ...] = ("confirming", "researching")

# 档位（§7.6）：子问题数照上游 auto 分解那档（2/4/6），每子问题的检索轮数按档位给
# ——deep 的 6 个满足验收「deep 档子问题 ≥5 个」
Depth = Literal["quick", "standard", "deep"]
DEPTHS: tuple[str, ...] = ("quick", "standard", "deep")

# 模式（§7.6）：report 出结构化报告 / answer 直接回答（同一套四阶段，只换报告段的提示词）
Mode = Literal["report", "answer"]
MODES: tuple[str, ...] = ("report", "answer")

# 在飞的调研过了这个时长就不再拦路：一张三天前的大纲卡不该把会话永远钉在 confirming 上
ACTIVE_TTL_S = 24 * 3600


class DepthSpec(BaseModel):
    """一个档位跑多少活。"""

    subtopics: int  # 拆几个子问题
    tool_rounds: int  # 每个子问题最多几轮「带工具的 LLM 调用」
    report_tokens: int  # 报告段输出上限


# quick 的工具轮数**不能是 1**：一轮用完就没有第二次机会，搜索通道返回一屏不相关的结果
# （免密钥必应中文版会把「2025」「long」当词条，回百科/词典页）时模型只能如实写「检索失败」。
# 2 轮 = 首轮 + 一次换词/换工具的重试，实测真机跑从「整段没证据」变成能捞到真来源。
DEPTH_SPECS: dict[str, DepthSpec] = {
    "quick": DepthSpec(subtopics=2, tool_rounds=2, report_tokens=4096),
    "standard": DepthSpec(subtopics=4, tool_rounds=3, report_tokens=6144),
    "deep": DepthSpec(subtopics=6, tool_rounds=5, report_tokens=8192),
}

MAX_TOPIC_CHARS = 500
MAX_SUBTOPIC_TITLE_CHARS = 120
MAX_SUBTOPIC_OVERVIEW_CHARS = 400


class SubTopic(BaseModel):
    """一个子问题 = 一次 `run_agent_loop`。"""

    title: str = ""
    overview: str = ""


class ResearchRun(BaseModel):
    """库里那一行。`subtopics` 是 JSON 列的解析结果。"""

    id: str
    session_id: str
    topic: str = ""
    refined_topic: str = ""
    mode: str = "report"
    depth: str = "standard"
    subtopics: list[SubTopic] = Field(default_factory=list)
    status: str = "confirming"
    failed_subtopics: list[str] = Field(default_factory=list)
    answer_message_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @staticmethod
    def new(
        *,
        session_id: str,
        topic: str,
        refined_topic: str,
        mode: str,
        depth: str,
        subtopics: list[SubTopic],
        status: str = "confirming",
        now: float | None = None,
    ) -> "ResearchRun":
        now = time.time() if now is None else now
        return ResearchRun(
            id=new_id("rrun"),
            session_id=session_id,
            topic=topic[:MAX_TOPIC_CHARS],
            refined_topic=refined_topic[:MAX_TOPIC_CHARS],
            mode=mode,
            depth=depth,
            subtopics=subtopics,
            status=status,
            created_at=now,
            updated_at=now,
        )

    @property
    def spec(self) -> DepthSpec:
        """本档位跑多少活（未知档位回落 standard——入库的值一定在 DEPTHS 里，防手改库）。"""
        return DEPTH_SPECS.get(self.depth, DEPTH_SPECS["standard"])

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


def parse_subtopics(raw: Any) -> list[SubTopic]:
    """宽容解析库里那列 JSON：坏行不该让整个会话打不开。"""
    if not isinstance(raw, list):
        return []
    items: list[SubTopic] = []
    for entry in raw:
        if isinstance(entry, str):
            items.append(SubTopic(title=entry.strip()[:MAX_SUBTOPIC_TITLE_CHARS]))
        elif isinstance(entry, dict):
            items.append(
                SubTopic(
                    title=str(entry.get("title") or "").strip()[:MAX_SUBTOPIC_TITLE_CHARS],
                    overview=str(entry.get("overview") or "").strip()[:MAX_SUBTOPIC_OVERVIEW_CHARS],
                )
            )
    return [item for item in items if item.title]
