"""记忆文本守卫（§7.10）：绝对化句式（banned）与长度截断。

- 记忆是「关于用户的长期结论」，最怕模型把一次性状态写成永恒判断（「用户总是
  跳过推导」）——一旦入库就会一直误导后续回合。banned 列表只收最强的绝对化词，
  中英各自维护；不确定的表述（倾向/常见/目前）不受影响。
- 引号/括号内的整段引用豁免：用户在对话里说「我从来不背公式」被转述时不算
  模型自己在下绝对判断。成对符号直接 toggle，不做嵌套配对（够用且好测）。
"""

import re

_BANNED_ZH = ("总是", "从不", "从来不", "永远", "绝不", "绝对是", "百分之百")
_BANNED_EN = ("always", "never", "absolutely")

_QUOTE_CHARS = "「」『』“”‘’\"'()（）"
_EN_PATTERNS = tuple(
    (term, re.compile(rf"(?<![a-z]){re.escape(term)}(?![a-z])", re.IGNORECASE))
    for term in _BANNED_EN
)


def strip_quoted(text: str) -> str:
    """去掉引号/括号区域后的可见文本（成对符号 toggle）。"""
    out: list[str] = []
    depth = 0
    for char in text:
        if char in _QUOTE_CHARS:
            depth = 1 - depth
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def find_banned(text: str) -> str | None:
    """命中绝对化句式返回该词（供计数/日志），干净返回 None。"""
    visible = strip_quoted(text)
    for term in _BANNED_ZH:
        if term in visible:
            return term
    for term, pattern in _EN_PATTERNS:
        if pattern.search(visible):
            return term
    return None


def truncate(text: str, limit: int) -> str:
    """按字符硬截断（条目上限 L2 400 / L3 300）。"""
    text = text.strip()
    return text if len(text) <= limit else text[:limit]
