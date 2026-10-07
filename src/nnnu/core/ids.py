"""ID 生成（§8.2）：模块前缀-8 位随机 hex，如 sess-ab12cd34。"""

import secrets

# §8.2 约定：ID 用「模块前缀-{8位随机}」；随阶段推进按需扩展
KNOWN_PREFIXES = (
    "sess",
    "msg",
    "turn",
    "evt",
    "ask",
    "att",
    "cron",
    "kb",
    "kbdoc",
    "nb",
    "nbr",
    "q",
    "qa",
    "lpath",
    "lnode",
    "lint",  # 答题交互行（learning_interactions）
    "rrun",  # 一次调研的草稿本（research_runs）
    "rnd",  # 一次渲染的产物目录（visualize / math_animator）
    "qrun",  # 一次题库 AI 操作（分类/出题）的记账批次（合成 turn_id 用）
    "mem",  # 记忆 L2/L3 条目（stable entry id，§7.10）
    "mrun",  # 一次记忆整合（consolidator run，§7.10）
    "cw",  # Co-Writer 文档（co_writer_docs，§7.13）
    "cwe",  # 一次待确认的改写（内存态，不落库；仅统一 edit_id 形状）
)


def new_id(prefix: str) -> str:
    """生成模块前缀 ID；未知前缀抛错（显式优于静默拼错）。"""
    if prefix not in KNOWN_PREFIXES:
        raise ValueError(f"未知 ID 前缀 {prefix!r}，已知：{KNOWN_PREFIXES}")
    return f"{prefix}-{secrets.token_hex(4)}"
