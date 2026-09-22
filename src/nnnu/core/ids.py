"""ID 生成（§8.2）：模块前缀-8 位随机 hex，如 sess-ab12cd34。"""

import secrets

# §8.2 约定：ID 用「模块前缀-{8位随机}」；随阶段推进按需扩展
KNOWN_PREFIXES = ("sess", "msg", "turn", "evt", "ask", "att", "cron", "kb", "kbdoc")


def new_id(prefix: str) -> str:
    """生成模块前缀 ID；未知前缀抛错（显式优于静默拼错）。"""
    if prefix not in KNOWN_PREFIXES:
        raise ValueError(f"未知 ID 前缀 {prefix!r}，已知：{KNOWN_PREFIXES}")
    return f"{prefix}-{secrets.token_hex(4)}"
