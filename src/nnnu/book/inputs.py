"""Book 素材汇集（§7.14）：勾选的知识库 / 笔记本 / 题库 / 会话 → 一份带引用表的 digest。

digest 是 spine 生成与估算的输入（编译期另有按章检索，见 compile 侧）：
- 每类素材有各自的字符上限，四段之和 = `MATERIAL_BUDGET_CHARS`（互不挤占，也不爆 prompt）；
- 每段素材同时产出一条 `MaterialItem`（kind/ref/label/正文片段）——ref 是唯一标识
  （KB 文档 id、记录 id、题目 id、会话 id），label 给人看；spine 里章节的
  source_refs 只许引用这里出现过的 ref（见 spine.py 的收编）。
- 库没就绪、文档没解析完、会话找不到……一律跳过（素材是「能用的才带」），
  一类都带不出来由调用方判为素材不可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from nnnu.book.models import MATERIAL_BUDGET_CHARS

if TYPE_CHECKING:
    from nnnu.book.models import Chapter
    from nnnu.services.knowledge.service import KBService
    from nnnu.services.notebooks.service import NotebookService
    from nnnu.services.question_bank.service import QuestionBankService
    from nnnu.services.sessions.service import SessionManager

# 四段上限（和 = 总预算）；KB 最重，会话只是补充语境
KB_SECTION_CHARS = 12_000
NOTEBOOK_SECTION_CHARS = 5_000
QUESTION_SECTION_CHARS = 4_000
SESSION_SECTION_CHARS = 3_000

KB_DOCS_PER_KB = 4  # 每库最多带几份文档的头部
KB_DOC_CHARS = 3_000
NOTEBOOK_RECORDS_PER_BOOK = 5
NOTEBOOK_RECORD_CHARS = 1_200
QUESTION_COUNT = 30
QUESTION_CHARS = 400
SESSION_MESSAGES = 24  # 每会话最近几条往来
SESSION_MESSAGE_CHARS = 300

# 段上限必须正好用满总预算（改数时一起改；上面四段之和 = MATERIAL_BUDGET_CHARS）

# 素材段与提示词的贴标语言（正文本身是什么语言就什么语言，只翻包装词）
_LABELS: dict[str, dict[str, str]] = {
    "zh": {
        "kb": "知识库",
        "notebook": "笔记本",
        "question": "题目",
        "session": "会话",
        "answer": "答案",
    },
    "en": {
        "kb": "Knowledge base",
        "notebook": "Notebook",
        "question": "Question",
        "session": "Session",
        "answer": "Answer",
    },
}


def _labels(language: str) -> dict[str, str]:
    return _LABELS.get(language, _LABELS["zh"])


def _head(text: Any, limit: int) -> str:
    """取正文头部并按上限截断（截断处补省略号，让模型知道后面还有）。"""
    if not isinstance(text, str):
        return ""
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


@dataclass(slots=True)
class MaterialItem:
    kind: str  # kb | notebook | question | session
    ref: str
    label: str
    text: str

    def render(self, index: int, labels: dict[str, str]) -> str:
        return f"[{index}] {labels.get(self.kind, self.kind)}·{self.label}\n{self.text}"


@dataclass(slots=True)
class MaterialDigest:
    items: list[MaterialItem] = field(default_factory=list)
    text: str = ""
    chars: int = 0

    def ref_labels(self) -> dict[str, str]:
        """ref → label（spine 的 source_refs 收编白名单）。"""
        return {item.ref: item.label for item in self.items if item.ref}

    def refs_block(self, language: str = "zh") -> str:
        """给提示词的素材清单：一条一行「- [kind] ref → label」。"""
        lines = []
        for kind, ref, label in self.distinct_refs():
            lines.append(f"- [{kind}] {ref} → {label}")
        return "\n".join(lines)

    def distinct_refs(self) -> list[tuple[str, str, str]]:
        seen: set[str] = set()
        result: list[tuple[str, str, str]] = []
        for item in self.items:
            if item.ref and item.ref not in seen:
                seen.add(item.ref)
                result.append((item.kind, item.ref, item.label))
        return result


class _Section:
    """一段素材的小账本：限额内追加条目，满了就停。"""

    def __init__(self, budget: int) -> None:
        self.items: list[MaterialItem] = []
        self.left = budget

    def add(self, kind: str, ref: str, label: str, text: str) -> None:
        text = text.strip()
        if not text or self.left <= 0 or not ref:
            return
        if len(text) > self.left:
            text = text[: self.left].rstrip() + "…"
        self.items.append(MaterialItem(kind=kind, ref=ref, label=label, text=text))
        self.left -= len(text)


async def collect_materials(
    sources: dict[str, Any],
    *,
    kb_service: "KBService",
    notebook_service: "NotebookService",
    question_service: "QuestionBankService",
    session_manager: "SessionManager",
    language: str = "zh",
) -> MaterialDigest:
    """按素材选择汇集 digest（sources 已由 validate_sources 规范化）。"""
    kb_section = _Section(KB_SECTION_CHARS)
    for kind, ref, label, text in _kb_entries(kb_service, sources.get("kbs") or []):
        kb_section.add(kind, ref, label, text)

    notebook_section = _Section(NOTEBOOK_SECTION_CHARS)
    for kind, ref, label, text in await _notebook_entries(
        notebook_service, sources.get("notebooks") or []
    ):
        notebook_section.add(kind, ref, label, text)

    question_section = _Section(QUESTION_SECTION_CHARS)
    for kind, ref, label, text in await _question_entries(
        question_service, sources.get("questions"), language
    ):
        question_section.add(kind, ref, label, text)

    session_section = _Section(SESSION_SECTION_CHARS)
    for kind, ref, label, text in await _session_entries(
        session_manager, sources.get("sessions") or []
    ):
        session_section.add(kind, ref, label, text)

    items = [
        *kb_section.items,
        *notebook_section.items,
        *question_section.items,
        *session_section.items,
    ]
    labels = _labels(language)
    text = "\n\n".join(item.render(index, labels) for index, item in enumerate(items, start=1))
    return MaterialDigest(items=items, text=text, chars=len(text))


def _kb_entries(
    kb_service: "KBService", kb_ids: Iterable[str]
) -> Iterator[tuple[str, str, str, str]]:
    """KB 条目：每库一条库级引用 + 每文档一条（正文取解析缓存头部）。"""
    for kb_id in kb_ids:
        manifest = kb_service.get_kb(kb_id)
        if manifest is None or manifest.status != "ready":
            continue
        yield ("kb", manifest.id, manifest.name, f"库内共 {len(manifest.live_docs())} 份文档")
        for doc in manifest.live_docs()[:KB_DOCS_PER_KB]:
            parsed = kb_service.doc_parsed(kb_id, doc.doc_id)
            body = parsed.get("text") if isinstance(parsed, dict) else None
            head = _head(body, KB_DOC_CHARS)
            if not head:
                continue
            yield ("kb", doc.doc_id, f"{manifest.name}·{doc.filename}", head)


async def _notebook_entries(
    notebook_service: "NotebookService", notebook_ids: Iterable[str]
) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for notebook_id in notebook_ids:
        notebook = await notebook_service.get_notebook(notebook_id)
        if notebook is None:
            continue
        records = await notebook_service.list_records(notebook_id)
        for record in records[:NOTEBOOK_RECORDS_PER_BOOK]:
            head = _head(record.content_md, NOTEBOOK_RECORD_CHARS)
            if head:
                entries.append(("notebook", record.id, f"{notebook.name}·{record.title}", head))
    return entries


async def _question_entries(
    question_service: "QuestionBankService", questions: dict[str, Any] | None, language: str
) -> list[tuple[str, str, str, str]]:
    if not questions:
        return []
    labels = _labels(language)
    filt = questions.get("filter", "all")
    if filt == "ids":
        items = []
        for question_id in list(questions.get("ids") or [])[:QUESTION_COUNT]:
            question = await question_service.get_question(question_id)
            if question is not None:
                items.append(question)
    else:
        items = await question_service.list_questions(filter=filt, limit=QUESTION_COUNT)
    entries: list[tuple[str, str, str, str]] = []
    for question in items:
        body = f"{question.stem}\n（{labels['answer']}：{question.answer}）"
        entries.append(
            ("question", question.id, question.knowledge_point or question.stem[:40], body)
        )
    return entries


async def _session_entries(
    session_manager: "SessionManager", session_ids: Iterable[str]
) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for session_id in session_ids:
        session = await session_manager.get_session(session_id)
        if session is None:
            continue
        messages = await session_manager.list_messages(session_id)
        recent = [message for message in messages if message.role in ("user", "assistant")]
        recent = recent[-SESSION_MESSAGES:]
        body = "\n".join(
            f"{message.role}: {_head(message.content, SESSION_MESSAGE_CHARS)}"
            for message in recent
            if message.content.strip()
        )
        if body:
            entries.append(("session", session.id, session.title or session.id, body))
    return entries


# ---- 编译期按章汇集 ----

KB_CHAPTER_TOP_K = 8  # 每库按章题检索取几条片段
KB_CHAPTER_HIT_CHARS = 1_200  # 单片段的字符上限


def _chapter_budgets(budget_chars: int) -> dict[str, int]:
    """章预算按整书各段占比摊：与 estimate 的「整书素材按章均分」口径一致。"""
    ratio = budget_chars / MATERIAL_BUDGET_CHARS
    return {
        "kb": int(KB_SECTION_CHARS * ratio),
        "notebook": int(NOTEBOOK_SECTION_CHARS * ratio),
        "question": int(QUESTION_SECTION_CHARS * ratio),
        "session": int(SESSION_SECTION_CHARS * ratio),
    }


async def _chapter_kb_entries(
    kb_service: "KBService", kb_ids: Iterable[str], query: str
) -> list[tuple[str, str, str, str]]:
    """按章题检索的 KB 片段；库里检不出东西就退回文档头部（小库也有材料可用）。"""
    entries: list[tuple[str, str, str, str]] = []
    for kb_id in kb_ids:
        manifest = kb_service.get_kb(kb_id)
        if manifest is None or manifest.status != "ready":
            continue
        names = {doc.doc_id: doc.filename for doc in manifest.live_docs()}
        hits = await kb_service.search(kb_id, query, top_k=KB_CHAPTER_TOP_K)
        if hits:
            for hit in hits:
                label = f"{manifest.name}·{names.get(hit.doc_id, hit.doc_id)}"
                entries.append(("kb", hit.doc_id, label, _head(hit.text, KB_CHAPTER_HIT_CHARS)))
            continue
        for doc in manifest.live_docs()[:2]:
            parsed = kb_service.doc_parsed(kb_id, doc.doc_id)
            body = parsed.get("text") if isinstance(parsed, dict) else None
            head = _head(body, KB_DOC_CHARS)
            if head:
                entries.append(("kb", doc.doc_id, f"{manifest.name}·{doc.filename}", head))
    return entries


async def collect_chapter_material(
    sources: dict[str, Any],
    chapter: "Chapter",
    *,
    kb_service: "KBService",
    notebook_service: "NotebookService",
    question_service: "QuestionBankService",
    session_manager: "SessionManager",
    language: str = "zh",
    budget_chars: int = MATERIAL_BUDGET_CHARS,
) -> MaterialDigest:
    """编译一章时给块生成用的素材（章节题的 KB 检索片段 + 其余三类整份）。

    KB 走检索（章题/摘要当 query）：比整库头部 digest 更贴这一章要写的内容；
    笔记本/题库/会话沿用整份条目——它们本来就短，按章筛反而容易筛没了。
    """
    query = f"{chapter.title} {chapter.summary}".strip() or chapter.title
    budgets = _chapter_budgets(budget_chars)

    kb_section = _Section(budgets["kb"])
    for kind, ref, label, text in await _chapter_kb_entries(
        kb_service, sources.get("kbs") or [], query
    ):
        kb_section.add(kind, ref, label, text)

    notebook_section = _Section(budgets["notebook"])
    for kind, ref, label, text in await _notebook_entries(
        notebook_service, sources.get("notebooks") or []
    ):
        notebook_section.add(kind, ref, label, text)

    question_section = _Section(budgets["question"])
    for kind, ref, label, text in await _question_entries(
        question_service, sources.get("questions"), language
    ):
        question_section.add(kind, ref, label, text)

    session_section = _Section(budgets["session"])
    for kind, ref, label, text in await _session_entries(
        session_manager, sources.get("sessions") or []
    ):
        session_section.add(kind, ref, label, text)

    items = [
        *kb_section.items,
        *notebook_section.items,
        *question_section.items,
        *session_section.items,
    ]
    labels = _labels(language)
    text = "\n\n".join(item.render(index, labels) for index, item in enumerate(items, start=1))
    return MaterialDigest(items=items, text=text, chars=len(text))
