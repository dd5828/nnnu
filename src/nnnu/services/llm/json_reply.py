"""从模型回复里取结构化 JSON（判分器与出题能力共用）。

模型不会老实只回一段 JSON：常见形状是「一句解释 + ```json 代码块」，LaTeX 还爱在
字符串里写反斜杠——`\\frac` 会被 JSON 当成 `\f` 转义吃掉，静默变成换页符 + "rac"。
这里只做**确定性**修复，修不好返回 None 交给调用方的兜底路径，绝不猜内容：

1. 解析前：裸控制字符（JSON 字符串里不允许）按 LaTeX 命令还原成 `\\f` 写法，
   非数学环境的按 JSON 转义写回；尾逗号；非法反斜杠转义翻倍；
2. 解析后：字符串值里残留的 `\x0c`/`\x08` 这类控制字符还原成 `\\f`/`\\b` 文本
   （即上面第 1 步没能拦住的那部分）。
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
# JSON 合法的转义字符；其它位置的反斜杠一律按字面量翻倍
_VALID_ESCAPES = set('"\\/bfnrtu')

# 控制字符 → 原本的转义写法：模型把 \frac 写成 "\frac" 时，JSON 会解析成换页符 + "rac"
_CONTROLS = "\x07\x08\x09\x0a\x0b\x0c\x0d"
# 解析前：把这些控制字符还原成「JSON 里的 LaTeX 字面量」（文本要写 \\f 三字符）
_PREPARSE_RESTORE = {
    "\x07": "\\\\a",
    "\x08": "\\\\b",
    "\x09": "\\\\t",
    "\x0a": "\\\\n",
    "\x0b": "\\\\v",
    "\x0c": "\\\\f",
    "\x0d": "\\\\r",
}
# 解析前：不还原的控制字符写成 JSON 转义（两字符），保住模型想表达的内容
_JSON_ESCAPE = {
    "\x07": "\\u0007",
    "\x08": "\\b",
    "\x09": "\\t",
    "\x0a": "\\n",
    "\x0b": "\\u000b",
    "\x0c": "\\f",
    "\x0d": "\\r",
}
# 解析后：字符串值里的控制字符还原成 LaTeX 写法（两字符，此时已在值里）
_POSTPARSE_RESTORE = {
    "\x07": "\\a",
    "\x08": "\\b",
    "\x09": "\\t",
    "\x0a": "\\n",
    "\x0b": "\\v",
    "\x0c": "\\f",
    "\x0d": "\\r",
}
# 正文里不可能真出现的（响铃/退格/垂直制表/换页）——一律还原
_ALWAYS_RESTORE = ("\x07", "\x08", "\x0b", "\x0c")
# 制表/换行/回车也可能是模型真想要的排版——只在 $ 数学环境里才当 LaTeX 命令还原
_MATH_RESTORE = ("\x09", "\x0a", "\x0d")


def json_candidates(text: str) -> list[str]:
    """候选 JSON 文本，按可信度排序：代码块（后→前）→ 整段 → 首尾括号包住的片段。"""
    raw = (text or "").strip()
    candidates = [block.strip() for block in reversed(_FENCE.findall(raw)) if block.strip()]
    if raw:
        candidates.append(raw)
    start = min((pos for pos in (raw.find("{"), raw.find("[")) if pos >= 0), default=-1)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    return candidates


def _strip_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA.sub(r"\1", text)


def _fix_stray_backslashes(text: str) -> str:
    """把非法的反斜杠转义（LaTeX 命令）翻倍，让 JSON 能解析。

    `\\u` 只在后面跟 4 位十六进制时才算合法转义——否则 `\\upsilon` 会被当成
    残缺的 unicode 转义，解析直接失败。
    """
    pieces: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            pieces.append(char)
            index += 1
            continue
        nxt = text[index + 1]
        unicode_escape = nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[index + 2 : index + 6])
        if nxt in _VALID_ESCAPES and (nxt != "u" or unicode_escape):
            pieces.append(char + nxt)
        else:
            pieces.append("\\\\" + nxt)
        index += 2
    return "".join(pieces)


def _restore_controls(text: str, table: dict[str, str], *, keep: bool) -> str:
    """按 `$` 数学环境把控制字符还原成 LaTeX 命令写法（\\f rac → \\frac）。

    不还原的那些：`keep=True` 时原样保留（解析后阶段——它已经是值里的真字符），
    否则写成 JSON 转义（解析前阶段——裸控制字符会让 json 直接报非法字符）。
    """
    if not any(char in _CONTROLS for char in text):
        return text
    pieces: list[str] = []
    in_math = False
    for index, char in enumerate(text):
        if char == "$":
            in_math = not in_math
            pieces.append(char)
            continue
        if char not in table:
            pieces.append(char)
            continue
        nxt = text[index + 1] if index + 1 < len(text) else ""
        restore = char in _ALWAYS_RESTORE or (char in _MATH_RESTORE and in_math)
        if restore and (not nxt or (nxt.isascii() and nxt.isalpha())):
            pieces.append(table[char])
        elif keep:
            pieces.append(char)
        else:
            pieces.append(_JSON_ESCAPE[char])
    return "".join(pieces)


def _fix_raw_controls(text: str) -> str:
    """解析前修复：JSON 字符串里的裸控制字符不能留（json 会直接报非法字符）。"""
    return _restore_controls(text, _PREPARSE_RESTORE, keep=False)


def _restore_values(value: Any) -> Any:
    """解析后修复：字符串值里被转义吃掉的控制字符还原成 LaTeX 写法。"""
    if isinstance(value, str):
        return _restore_controls(value, _POSTPARSE_RESTORE, keep=True)
    if isinstance(value, list):
        return [_restore_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _restore_values(item) for key, item in value.items()}
    return value


def loads_lenient(text: str) -> Any | None:
    """宽松解析单个 JSON 文本；几种修法都试过还不行返回 None。"""
    cleaned = _strip_trailing_commas(text)
    escaped = _fix_stray_backslashes(cleaned)
    attempts = (
        text,
        cleaned,
        _fix_stray_backslashes(text),
        escaped,
        _fix_raw_controls(escaped),
    )
    for attempt in attempts:
        try:
            return _restore_values(json.loads(attempt))
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_json_reply(text: str) -> Any | None:
    """回复文本 → JSON 值；一个候选都解析不出来返回 None。"""
    for candidate in json_candidates(text):
        parsed = loads_lenient(candidate)
        if parsed is not None:
            return parsed
    return None
