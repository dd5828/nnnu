"""会话导出（§7.1）：完整 Markdown 转录——消息 + 思考 + 工具轨迹 + 引用 + 成本。

纯函数：输入 Session + 消息列表，输出 Markdown 文本；标签按会话语言双语。
"""

from datetime import datetime
from typing import Any

from nnnu.services.sessions.models import Message, Session

_ROLE_LABELS = {
    "user": {"zh": "用户", "en": "User"},
    "assistant": {"zh": "助手", "en": "Assistant"},
}
_TOOL_OK = {"zh": "成功", "en": "ok"}
_TOOL_FAIL = {"zh": "失败", "en": "failed"}
_CITATION = {"zh": "引用", "en": "citation"}
_COST = {"zh": "成本", "en": "cost"}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_cost(cost: dict[str, Any] | None, lang: str) -> str:
    if not cost:
        return ""
    tokens = cost.get("tokens", 0)
    money = f"${cost.get('cost', 0):.4f}".rstrip("0").rstrip(".")
    return f"> {_COST[lang]}：{tokens} tokens / {money}"


def export_session_markdown(session: Session, messages: list[Message]) -> str:
    """§7.1 完整转录：思考块折叠为引用、工具轨迹与引用逐条列出。"""
    lang = session.language if session.language in _ROLE_LABELS["user"] else "zh"
    title = session.title or session.id
    lines = [
        f"# {title}",
        "",
        f"> {_fmt_time(session.updated_at)} · {session.id} · {len(messages)} 条消息",
        "",
    ]
    for message in messages:
        role_label = _ROLE_LABELS.get(message.role, {}).get(lang, message.role)
        lines.append(f"## {role_label} · {_fmt_time(message.created_at)}")
        lines.append("")
        if message.thinking:
            lines.append("> 思考：")
            for line in message.thinking.splitlines() or [""]:
                lines.append(f"> {line}")
            lines.append("")
        if message.content:
            lines.append(message.content)
            lines.append("")
        for call in message.tool_calls or []:
            status = _TOOL_OK[lang] if call.get("ok") else _TOOL_FAIL[lang]
            lines.append(f"> 🔧 工具：{call.get('tool_name', '')}（{status}）")
            summary = str(call.get("summary", "")).replace("\n", " ")
            if summary:
                lines.append(f"> {summary}")
        for citation in message.citations or []:
            page = f" 第{citation.get('page')}页" if citation.get("page") else ""
            snippet = str(citation.get("snippet", "")).replace("\n", " ")
            lines.append(f"> 📖 {_CITATION[lang]}：{citation.get('doc_id', '')}{page}：{snippet}")
        lines.append(_fmt_cost(message.cost, lang))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
