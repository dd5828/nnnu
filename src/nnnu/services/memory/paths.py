"""记忆存储路径与常量（§7.10 / §8.1）：data/user/memory/ 下的三层次布局。

- trace/<surface>/<yyyy-mm>.jsonl：L1 事件轨迹（append-only，按月分片；
  §8.3 写「按天」，但 §7.10/§8.1 两处与引用示例 `[L1:chat/2026-09.jsonl#123]`
  都是按月——取按月，行号引用才稳定，见 STAGE_LOG 偏离记录）；
- L2/<surface>.md：分面事实（每条带 [L1 引用]）；
- L3/{profile,recent,scope,preferences}.md：跨面画像（每条带 [L2 引用]）；
- state.json：consolidator 水位与人工编辑哈希（§8.1）。

SURFACES 与各能力 manifest.name 一一对齐（tests/test_memory_trace.py 有锁）：
L1 只在「回合」上落——非回合面（notebook REST / 判分 / KB 构建 / 共写）
不在此列，属 P8 已知范围（记遗留）。
"""

from pathlib import Path

SURFACES: tuple[str, ...] = (
    "chat",
    "deep_solve",
    "deep_question",
    "mastery_path",
    "deep_research",
    "visualize",
    "math_animator",
)

L3_DOCS: tuple[str, ...] = ("profile", "recent", "scope", "preferences")

# L3 各文档的中文标题（建新文件时用；改动不迁移既有文件头）
L3_TITLES: dict[str, str] = {
    "profile": "用户画像",
    "recent": "近期动态",
    "scope": "知识范围",
    "preferences": "偏好",
}


def memory_root(data_root: Path) -> Path:
    return data_root / "user" / "memory"


def trace_dir(data_root: Path, surface: str) -> Path:
    return memory_root(data_root) / "trace" / surface


def trace_path(data_root: Path, surface: str, year_month: str) -> Path:
    return trace_dir(data_root, surface) / f"{year_month}.jsonl"


def l2_path(data_root: Path, surface: str) -> Path:
    return memory_root(data_root) / "L2" / f"{surface}.md"


def l3_path(data_root: Path, doc: str) -> Path:
    return memory_root(data_root) / "L3" / f"{doc}.md"


def state_path(data_root: Path) -> Path:
    return memory_root(data_root) / "state.json"


def semantic_path(data_root: Path) -> Path:
    """语义知识图谱派生工件（§7.10 补做）：extract 模式写，图谱 API 读。"""
    return memory_root(data_root) / "semantic.json"
