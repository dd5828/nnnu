"""笔记本导出（§7.15「Markdown 导出」）：整本一个 Markdown 文件。

按创建时间升序——导出是给人从头读的，和界面列表的「新的在前」相反。
标签按语言双语（会话导出的同款做法，语言由调用方取界面语言传入）。
"""

from datetime import datetime

from nnnu.services.notebooks.models import Notebook, NotebookRecord

_TYPE_LABELS: dict[str, dict[str, str]] = {
    "chat": {"zh": "对话", "en": "Chat"},
    "solve": {"zh": "解答", "en": "Solution"},
    "note": {"zh": "笔记", "en": "Note"},
    "question": {"zh": "题目", "en": "Question"},
    "research": {"zh": "研究报告", "en": "Research"},
    "visualize": {"zh": "图表", "en": "Visualization"},
    "math_animator": {"zh": "数学动画", "en": "Math animation"},
}
_RECORDS = {"zh": "条记录", "en": "records"}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def export_notebook_markdown(
    notebook: Notebook, records: list[NotebookRecord], lang: str = "zh"
) -> str:
    if lang not in _TYPE_LABELS["note"]:
        lang = "zh"
    lines = [f"# {notebook.name}", ""]
    if notebook.description:
        lines += [f"> {notebook.description}", ""]
    lines += [f"> {len(records)} {_RECORDS[lang]} · {_fmt_time(notebook.created_at)}", ""]
    for record in sorted(records, key=lambda item: item.created_at):
        label = _TYPE_LABELS.get(record.type, {}).get(lang, record.type)
        meta = f"{label} · {_fmt_time(record.created_at)}"
        if record.source_ref:
            meta += f" · {record.source_ref}"
        lines += [f"## {record.title}", "", f"> {meta}", "", record.content_md, ""]
    return "\n".join(lines).rstrip() + "\n"
