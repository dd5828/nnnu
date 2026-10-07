"""整书导出（§7.14「导出 Markdown 结构完整」）：章/块摊成一份从头读的 Markdown。

- 章按 spine 序；块按页内序；没生成完的（pending/compiling/error/空 payload）跳过；
- quiz 的题目留在章内、答案与解析汇到章末「参考答案」（先想再看，纸质书惯例）；
- figure 家族的 payload 本来就是含渲染围栏的 Markdown，原样保留——外部 Markdown
  阅读器能看到围栏源码，回 nnnu 里照旧渲染；
- 标签按语言双语，语言由调用方取界面语言传入（照 services/notebooks/export.py 手法）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nnnu.book.models import Block, Book, BookPage, Spine

_CALLOUT_LABELS = {
    "zh": {"info": "要点", "tip": "技巧", "warn": "易错", "important": "切记"},
    "en": {"info": "Note", "tip": "Tip", "warn": "Watch out", "important": "Important"},
}
_TEXTS: dict[str, dict[str, str]] = {
    "zh": {
        "meta": "{chapters} 章 · {date}",
        "answers": "参考答案",
        "extend": "延伸",
        "answer": "答案",
        "explanation": "解析",
        "when_sep": "：",
    },
    "en": {
        "meta": "{chapters} chapters · {date}",
        "answers": "Answers",
        "extend": "Going deeper",
        "answer": "Answer",
        "explanation": "Why",
        "when_sep": ": ",
    },
}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _text(raw: Any) -> str:
    return str(raw).strip() if raw is not None else ""


def block_markdown(block: Block, lang: str = "zh", *, include_answers: bool = False) -> str:
    """一个块 → Markdown 文本（空块回空串，调用方跳过）。

    include_answers=False（导出）：quiz 只出题目，答案由章末汇总；
    include_answers=True（页聊天的上下文）：答案与解析直接跟在题目后。
    """
    texts = _TEXTS.get(lang, _TEXTS["zh"])
    payload = block.payload or {}
    if block.type in ("text", "note"):
        return _text(payload.get("markdown"))
    if block.type == "callout":
        markdown = _text(payload.get("markdown"))
        label = _CALLOUT_LABELS.get(lang, _CALLOUT_LABELS["zh"]).get(
            str(payload.get("variant") or "info"), ""
        )
        return f"> **{label}**：{markdown}" if label and markdown else markdown
    if block.type == "deep_dive":
        markdown = _text(payload.get("markdown"))
        sources = [item.strip() for item in payload.get("sources") or [] if _text(item)]
        if markdown and sources:
            markdown += f"\n\n{texts['extend']}：" + "；".join(sources)
        return markdown
    if block.type == "quiz":
        lines = [f"**{_text(payload.get('stem'))}**", ""]
        for option in payload.get("options") or []:
            lines.append(f"- {_text(option.get('key'))}. {_text(option.get('text'))}")
        if include_answers:
            keys = "".join(_text(key) for key in payload.get("answer_key") or [])
            note = f"{texts['answer']}：{keys}"
            explanation = _text(payload.get("explanation"))
            if explanation:
                note += f"。{texts['explanation']}：{explanation}"
            lines += ["", f"（{note}）"]
        return "\n".join(lines).rstrip()
    if block.type == "flashcard":
        return "\n".join(
            f"- **{_text(card.get('front'))}** — {_text(card.get('back'))}"
            for card in payload.get("cards") or []
            if isinstance(card, dict)
        )
    if block.type == "timeline":
        return "\n".join(
            f"- **{_text(event.get('when'))}**{texts['when_sep']}{_text(event.get('title'))}"
            + (f" — {_text(event.get('detail'))}" if _text(event.get("detail")) else "")
            for event in payload.get("events") or []
            if isinstance(event, dict)
        )
    if block.type == "code":
        code = str(payload.get("code") or "")
        if not code.strip():
            return ""
        body = f"```{_text(payload.get('language')) or 'text'}\n{code.rstrip()}\n```"
        explanation = _text(payload.get("explanation"))
        return f"{body}\n\n{explanation}" if explanation else body
    if block.type in ("figure", "interactive_html", "animation"):
        return _text(payload.get("markdown"))
    if block.type == "concept_graph":
        nodes = {
            _text(node.get("id")): _text(node.get("label")) or _text(node.get("id"))
            for node in payload.get("nodes") or []
            if isinstance(node, dict)
        }
        edge_lines: list[str] = []
        used: set[str] = set()
        for edge in payload.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source, target = _text(edge.get("source")), _text(edge.get("target"))
            label = _text(edge.get("label"))
            edge_lines.append(
                f"- {nodes.get(source, source)} → {nodes.get(target, target)}"
                + (f" — {label}" if label else "")
            )
            used.update((source, target))
        edge_lines += [f"- {label}" for node_id, label in nodes.items() if node_id not in used]
        return "\n".join(edge_lines)
    return ""


def export_book_markdown(book: Book, spine: Spine, pages: list[BookPage], lang: str = "zh") -> str:
    """整书 Markdown：空章只留标题，正文全是 done 块拼的。"""
    if lang not in _TEXTS:
        lang = "zh"
    texts = _TEXTS[lang]
    pages_by_key = {page.chapter_key: page for page in pages}
    lines = [
        f"# {book.title}",
        "",
        f"> {texts['meta'].format(chapters=len(spine.chapters), date=_fmt_time(book.created_at))}",
        "",
    ]
    for chapter in spine.chapters:
        lines += [f"## {chapter.title}", ""]
        if chapter.summary:
            lines += [f"> {chapter.summary}", ""]
        page = pages_by_key.get(chapter.key)
        answers: list[Block] = []
        if page is not None:
            for block in page.blocks:
                if block.status != "done":
                    continue
                text = block_markdown(block, lang)
                if not text:
                    continue
                lines += [text, ""]
                if block.type == "quiz":
                    answers.append(block)
        if answers:
            lines += [f"### {texts['answers']}", ""]
            for index, block in enumerate(answers, start=1):
                keys = "".join(_text(key) for key in block.payload.get("answer_key") or [])
                explanation = _text(block.payload.get("explanation"))
                line = f"{index}. **{keys}**"
                if explanation:
                    line += f" — {explanation}"
                lines.append(line)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
