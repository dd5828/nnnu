"""记忆领域模型（§7.10）：L1 事件类型 / L2·L3 条目 / 整合运行 / 文本归一与哈希。

纯数据与纯函数，不碰 IO——IO 在 trace.py / store.py / state.py 里。
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

# §8.3 的 L1 事件种类：user_message | tool_call | assistant_done | ask_user | cost
EVENT_KINDS: tuple[str, ...] = ("user_message", "tool_call", "assistant_done", "ask_user", "cost")

# 条目来源（写者身份）：consolidator（L2/L3 整合）/ model（write_memory 工具）/ human（工作台）
ORIGINS: tuple[str, ...] = ("consolidator", "model", "human")


def normalize_text(text: str) -> str:
    """空白归一：比对/哈希口径统一（改排版不算改内容）。"""
    return " ".join(text.split())


def text_digest(text: str) -> str:
    """条目正文的 sha256（不含日期与引用——改引用/日期不算人工编辑）。"""
    return "sha256:" + hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class MemoryEntry:
    """L2/L3 文档里的一条 bullet 条目（格式见 store.py 头注）。"""

    id: str | None  # mem-xxxxxxxx；None = 手写未纳管（只读展示，整合时补 id）
    date: str  # YYYY-MM-DD
    text: str
    refs: list[str]  # ["L1:chat/2026-09.jsonl#123"] 或 ["L2:chat#mem-…", …]（无方括号）
    stale: bool = False
    origin: str = "consolidator"
    edited: bool = False  # 人工编辑保护开关（state.json 权威；这里是合流展示值）
    layer: str = "l2"  # l2 | l3
    key: str = ""  # 所属文件键（surface 或 L3 文档名）
    line_no: int = 0  # 文件内 1-based 行号（解析时钉，仅供展示）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "date": self.date,
            "text": self.text,
            "refs": list(self.refs),
            "stale": self.stale,
            "origin": self.origin,
            "edited": self.edited,
            "layer": self.layer,
            "key": self.key,
            "line": self.line_no,
        }


@dataclass(slots=True)
class MemoryDoc:
    """一份 L2/L3 文档：文件头 + 机器条目 + 保底原样行。"""

    layer: str  # l2 | l3
    key: str  # surface 或 L3 文档名
    header: str = ""  # 第一个条目之前的行（# 标题与空行），原样保留
    entries: list[MemoryEntry] = field(default_factory=list)
    # 解析不出条目结构的非空行（用户手写、格式跑偏）：改写时原样带在末尾，
    # 绝不因机器重排而丢内容
    extras: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemoryConfig:
    """记忆运行参数（§7.10 设置区 memory；默认值即规格默认）。

    from_mapping 做防御性夹取：设置区在刀四才落 SPECS，之前的调用方直接拿默认值。
    """

    auto_enabled: bool = True
    auto_threshold_turns: int = 20
    budget_update: int = 3
    budget_audit: int = 2
    budget_dedup: int = 2
    budget_extract: int = 2
    update_chunk_chars: int = 3000
    trace_enabled: bool = True
    inject_enabled: bool = True

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> "MemoryConfig":
        config = cls()
        for source in (values or {}).items():
            key, value = source
            if key in ("auto_enabled", "trace_enabled", "inject_enabled") and isinstance(
                value, bool
            ):
                setattr(config, key, value)
            elif key == "auto_threshold_turns" and isinstance(value, int):
                config.auto_threshold_turns = max(1, min(500, value))
            elif key in (
                "budget_update",
                "budget_audit",
                "budget_dedup",
                "budget_extract",
            ) and isinstance(value, int):
                setattr(config, key, max(0, min(20, value)))
            elif key == "update_chunk_chars" and isinstance(value, int):
                config.update_chunk_chars = max(500, min(8000, value))
        return config


@dataclass(slots=True)
class ConsolidationRun:
    """一次整合运行的状态（落 state.json 的 last_run，§7.10）。"""

    id: str
    trigger: str  # manual | auto
    status: str  # queued（后台已排队）| running | ok | error | interrupted
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stats: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stats": dict(self.stats),
            "events": list(self.events),
            "error": self.error,
        }
