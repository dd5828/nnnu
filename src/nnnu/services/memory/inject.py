"""首回合记忆注入（§7.10 补做）：把 L3 画像摘成一段附给模型的背景。

- 只读 + 纯拼接：表头走提示词渲染（带语言），条目正文一律直接拼接——L3 正文是
  自由文本，可能含花括号，喂 str.format 会炸；
- 条数与字数双上限：按行粒度截（宁可少一条，不把一句话切一半）；单条超长截断加省略号；
- 空记忆 / 全 stale → 返回空串，调用方据此不注入。
"""

from __future__ import annotations

from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.memory.store import MemoryStore

# 文档顺序：画像 → 偏好 → 范围 → 近期（越靠前越「长期稳定」）
INJECT_DOC_ORDER: tuple[str, ...] = ("profile", "preferences", "scope", "recent")
INJECT_PER_DOC = 4  # 每篇最多条数
INJECT_MAX_CHARS = 700  # 正文总上限（表头不计）
INJECT_ENTRY_MAX_CHARS = 120  # 单条上限


def build_injection(store: MemoryStore, lang: str) -> str:
    """生成注入块（表头 + 条目行）；没有可用条目返回 ""。"""
    lines: list[str] = []
    budget = INJECT_MAX_CHARS
    for doc_key in INJECT_DOC_ORDER:
        if budget <= 0:
            break
        doc = store.load("l3", doc_key)
        taken = 0
        for entry in doc.entries:
            if taken >= INJECT_PER_DOC or budget <= 0:
                break
            if entry.stale:  # 证据断链的条目不注入（audit 修好前不采信）
                continue
            text = entry.text.strip()
            if len(text) > INJECT_ENTRY_MAX_CHARS:
                text = text[:INJECT_ENTRY_MAX_CHARS] + "…"
            line = f"- {text}"
            if len(line) > budget:
                budget = 0  # 整行放不下：到此为止
                break
            lines.append(line)
            budget -= len(line)
            taken += 1
    if not lines:
        return ""
    header = get_prompt_manager().render("memory", lang, "inject.note").strip()
    return "\n".join([header, *lines]) if header else "\n".join(lines)
