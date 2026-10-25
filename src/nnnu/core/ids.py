"""ID 生成（§8.2）：模块前缀-8 位随机 hex，如 sess-ab12cd34。"""

import hashlib
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
    "bk",  # Book 活书（books，§7.14）
    "bp",  # 书的一页（book_pages；一章一页）
    "blk",  # 页内一个类型化块（存在 book_pages.blocks 的 JSON 里，不单表）
    "bmsg",  # 书页聊天的一条消息（book_page_messages，§8.2 外）
    "bat",  # 书页测验的一次作答（book_attempts，§8.2 外）
    "mcp",  # 一个 MCP 服务器配置（servers.json 里的键由用户起名，这个留作内部引用）
    "ag",  # 一条子代理连接配置（agents/connections.json，§7.12）——走 new_id
    "imp",  # 一条导入的第三方 CLI 历史会话（agent_sessions）——走 stable_id，不是 new_id
)


def new_id(prefix: str) -> str:
    """生成模块前缀 ID；未知前缀抛错（显式优于静默拼错）。"""
    if prefix not in KNOWN_PREFIXES:
        raise ValueError(f"未知 ID 前缀 {prefix!r}，已知：{KNOWN_PREFIXES}")
    return f"{prefix}-{secrets.token_hex(4)}"


def stable_id(prefix: str, key: str) -> str:
    """由 `key` 派生的**确定性** ID，形状与 new_id 一致（前缀-8 位 hex）。

    为什么不用 new_id：导入外部 CLI 历史会话时，同一个来源会话被扫到第二次必须
    落到同一个 id 上（幂等覆盖），随机串做不到。8 位 hex 是 32 位空间，来源 id
    本身又带来源前缀，撞车概率可忽略；真撞了也只是两条会话互相覆盖，导入是
    可重跑的（再扫一次按 digest 修正）。
    """
    if prefix not in KNOWN_PREFIXES:
        raise ValueError(f"未知 ID 前缀 {prefix!r}，已知：{KNOWN_PREFIXES}")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:8]}"
