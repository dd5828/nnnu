"""Book 素材汇集：四类素材的取数、限额截断、引用表、不可用时的空结果；
以及编译期按章汇集（KB 走检索、检不出来退文档头部、章预算按占比摊）。"""

from nnnu.book.inputs import (
    KB_CHAPTER_HIT_CHARS,
    KB_DOC_CHARS,
    KB_DOCS_PER_KB,
    QUESTION_COUNT,
    SESSION_MESSAGES,
    collect_chapter_material,
    collect_materials,
)
from nnnu.book.models import Chapter
from nnnu.services.knowledge.types import KbDoc, KbManifest
from nnnu.services.notebooks.models import Notebook, NotebookRecord
from nnnu.services.question_bank.models import Question
from nnnu.services.rag.base import Hit
from nnnu.services.sessions.models import Message, Session


class FakeKb:
    def __init__(
        self,
        kbs: list[KbManifest],
        parsed: dict[str, dict] | None = None,
        hits: dict[str, list[Hit]] | None = None,
    ) -> None:
        self._kbs = {kb.id: kb for kb in kbs}
        self._parsed = parsed or {}
        self._hits = hits or {}
        self.queries: list[tuple[str, str, int]] = []

    def get_kb(self, kb_id: str) -> KbManifest | None:
        return self._kbs.get(kb_id)

    def doc_parsed(self, kb_id: str, doc_id: str) -> dict | None:
        return self._parsed.get(doc_id)

    async def search(self, kb_id: str, query: str, *, mode: str = "hybrid", top_k: int = 5):
        self.queries.append((kb_id, query, top_k))
        return list(self._hits.get(kb_id, []))


class FakeNotebooks:
    def __init__(self, notebooks: list[Notebook], records: dict[str, list[NotebookRecord]]) -> None:
        self._notebooks = {item.id: item for item in notebooks}
        self._records = records

    async def get_notebook(self, notebook_id: str) -> Notebook | None:
        return self._notebooks.get(notebook_id)

    async def list_records(self, notebook_id: str) -> list[NotebookRecord]:
        return self._records.get(notebook_id, [])


class FakeQuestions:
    def __init__(self, questions: list[Question]) -> None:
        self._questions = {item.id: item for item in questions}

    async def get_question(self, question_id: str) -> Question | None:
        return self._questions.get(question_id)

    async def list_questions(self, *, filter: str, limit: int, **kwargs) -> list[Question]:
        self.last_filter = filter
        items = list(self._questions.values())
        if filter == "wrong":
            items = [item for item in items if item.wrong_count > 0]
        return items[:limit]


class FakeSessions:
    def __init__(self, sessions: dict[str, Session], messages: dict[str, list[Message]]) -> None:
        self._sessions = sessions
        self._messages = messages

    async def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def list_messages(self, session_id: str) -> list[Message]:
        return self._messages.get(session_id, [])


def _kb(kb_id: str = "kb-1", status: str = "ready", docs: int = 1) -> KbManifest:
    return KbManifest(
        id=kb_id,
        name="教材库",
        status=status,
        active_version=1 if status == "ready" else 0,
        docs=[
            KbDoc(doc_id=f"kbdoc-{index}", filename=f"第{index}章.md", status="done")
            for index in range(1, docs + 1)
        ],
    )


def _services(kb: FakeKb | None = None):
    return {
        "kb_service": kb or FakeKb([]),
        "notebook_service": FakeNotebooks([], {}),
        "question_service": FakeQuestions([]),
        "session_manager": FakeSessions({}, {}),
    }


async def _collect(sources: dict, **services):
    kwargs = {**_services(), **services}
    return await collect_materials(sources, language="zh", **kwargs)


async def test_kb_digest_has_kb_level_and_doc_level_refs():
    manifest = _kb(docs=2)
    kb = FakeKb(
        [manifest],
        {
            "kbdoc-1": {"text": "第一章讲傅里叶。", "kind": "text"},
            "kbdoc-2": {"text": "第二章讲滤波。"},
        },
    )
    digest = await _collect({"kbs": ["kb-1"]}, kb_service=kb)
    assert digest.chars == len(digest.text)
    refs = digest.ref_labels()
    assert refs["kb-1"] == "教材库"  # 库级引用（整库概括）
    assert refs["kbdoc-1"] == "教材库·第1章.md"
    assert "第一章讲傅里叶。" in digest.text
    # refs_block 里 ref 与 label 成对出现（提示词里 source_refs 只能从这取）
    assert "- [kb] kbdoc-1 → 教材库·第1章.md" in digest.refs_block()


async def test_kb_doc_count_is_capped():
    manifest = _kb(docs=6)
    kb = FakeKb([manifest], {f"kbdoc-{i}": {"text": f"正文 {i}"} for i in range(1, 7)})
    digest = await _collect({"kbs": ["kb-1"]}, kb_service=kb)
    doc_refs = [
        ref for kind, ref, _label in digest.distinct_refs() if kind == "kb" and ref != "kb-1"
    ]
    assert len(doc_refs) == KB_DOCS_PER_KB  # 只带前几份的头部


async def test_kb_doc_text_is_truncated_within_section_budget():
    manifest = _kb(docs=1)
    kb = FakeKb([manifest], {"kbdoc-1": {"text": "字" * 50_000}})
    digest = await _collect({"kbs": ["kb-1"]}, kb_service=kb)
    assert len(digest.text) < KB_DOC_CHARS + 200  # 长文只留头部


async def test_not_ready_kb_is_skipped():
    digest = await _collect(
        {"kbs": ["kb-1"]},
        kb_service=FakeKb([_kb(status="indexing")], {"kbdoc-1": {"text": "还没好"}}),
    )
    assert digest.items == []
    assert digest.text == ""


async def test_notebook_question_session_entries():
    notebook = Notebook.new(name="错题本")
    record = NotebookRecord.new(
        notebook_id=notebook.id, type="note", title="傅里叶笔记", content_md="记录内容：频域。"
    )
    question = Question(id="q-1", stem="傅里叶变换做什么？", answer="A", knowledge_point="信号")
    session = Session.new(title="聊傅里叶")
    messages = [
        Message.new(session_id=session.id, role="user", content="傅里叶是什么？"),
        Message.new(session_id=session.id, role="assistant", content="把信号拆成频率。"),
        Message.new(session_id=session.id, role="tool", content="（工具输出不带上）"),
    ]
    digest = await _collect(
        {
            "notebooks": [notebook.id],
            "questions": {"filter": "all", "ids": []},
            "sessions": [session.id],
        },
        notebook_service=FakeNotebooks([notebook], {notebook.id: [record]}),
        question_service=FakeQuestions([question]),
        session_manager=FakeSessions({session.id: session}, {session.id: messages}),
    )
    kinds = [item.kind for item in digest.items]
    assert kinds == ["notebook", "question", "session"]
    assert "记录内容：频域。" in digest.text
    assert "傅里叶变换做什么？" in digest.text and "（答案：A）" in digest.text
    assert "傅里叶是什么？" in digest.text and "工具输出" not in digest.text
    assert digest.ref_labels()[question.id] == "信号"


async def test_question_filter_ids_uses_selected_only():
    questions = FakeQuestions(
        [
            Question(id="q-1", stem="一", answer="A"),
            Question(id="q-2", stem="二", answer="B"),
        ]
    )
    digest = await _collect(
        {"questions": {"filter": "ids", "ids": ["q-2"]}}, question_service=questions
    )
    assert [item.ref for item in digest.items] == ["q-2"]


async def test_wrong_filter_passes_through_and_caps_count():
    questions = FakeQuestions(
        [
            Question(id=f"q-{index}", stem=f"题 {index}", answer="A", wrong_count=1)
            for index in range(QUESTION_COUNT + 5)
        ]
    )
    digest = await _collect({"questions": {"filter": "wrong"}}, question_service=questions)
    assert questions.last_filter == "wrong"
    assert len(digest.items) == QUESTION_COUNT


async def test_session_keeps_recent_messages_only():
    session = Session.new(title="长对话")
    messages = [
        Message.new(session_id=session.id, role="user", content=f"第 {index} 句")
        for index in range(SESSION_MESSAGES + 10)
    ]
    digest = await _collect(
        {"sessions": [session.id]},
        session_manager=FakeSessions({session.id: session}, {session.id: messages}),
    )
    assert "第 0 句" not in digest.text  # 老的放掉
    assert f"第 {SESSION_MESSAGES + 9} 句" in digest.text


async def test_missing_sources_yield_empty_digest():
    digest = await _collect({"kbs": ["kb-404"], "notebooks": ["nb-404"]})
    assert digest.items == [] and digest.chars == 0


async def test_english_labels():
    manifest = _kb(docs=1)
    kb = FakeKb([manifest], {"kbdoc-1": {"text": "Fourier transform."}})
    digest = await collect_materials({"kbs": ["kb-1"]}, language="en", **_services(kb))
    assert "Knowledge base" in digest.text


# ---- 编译期按章汇集（collect_chapter_material）----


def _chapter(title: str = "频率的世界", summary: str = "讲频谱怎么看") -> Chapter:
    return Chapter(key="ch-2", title=title, summary=summary)


async def _collect_chapter(sources: dict, chapter: Chapter | None = None, **services):
    kwargs = {**_services(), **services}
    return await collect_chapter_material(sources, chapter or _chapter(), language="zh", **kwargs)


async def test_chapter_material_searches_with_chapter_query():
    manifest = _kb(docs=2)
    hits = [
        Hit(doc_id="kbdoc-2", kb_id="kb-1", score=0.9, text="频谱横轴是频率。"),
        Hit(doc_id="kbdoc-1", kb_id="kb-1", score=0.5, text="傅里叶把信号拆开。"),
    ]
    kb = FakeKb([manifest], hits={"kb-1": hits})
    digest = await _collect_chapter({"kbs": ["kb-1"]}, kb_service=kb)
    # 检索的 query 是「章题 + 摘要」；命中片段进 digest，label 用文档文件名
    assert kb.queries == [("kb-1", "频率的世界 讲频谱怎么看", 8)]
    assert "频谱横轴是频率。" in digest.text
    assert digest.ref_labels()["kbdoc-2"] == "教材库·第2章.md"


async def test_chapter_hit_text_is_capped():
    manifest = _kb(docs=1)
    hits = [Hit(doc_id="kbdoc-1", kb_id="kb-1", score=0.9, text="字" * 30_000)]
    kb = FakeKb([manifest], hits={"kb-1": hits})
    digest = await _collect_chapter({"kbs": ["kb-1"]}, kb_service=kb)
    assert len(digest.text) < KB_CHAPTER_HIT_CHARS + 200


async def test_chapter_material_falls_back_to_doc_heads_without_hits():
    manifest = _kb(docs=1)
    kb = FakeKb([manifest], {"kbdoc-1": {"text": "第一章讲傅里叶。"}})  # 检索没有命中
    digest = await _collect_chapter({"kbs": ["kb-1"]}, kb_service=kb)
    assert "第一章讲傅里叶。" in digest.text
    assert digest.ref_labels()["kbdoc-1"] == "教材库·第1章.md"


async def test_chapter_material_scales_with_share_budget():
    manifest = _kb(docs=1)
    # 十条命中（每条都顶到单片 1200 上限）→ 段预算先满，总量被章预算卡住
    hits = [
        Hit(doc_id="kbdoc-1", kb_id="kb-1", score=0.9, text=f"片段{index}：" + "字" * 1_200)
        for index in range(10)
    ]
    kb = FakeKb([manifest], hits={"kb-1": hits})
    digest = await collect_chapter_material(
        {"kbs": ["kb-1"]},
        _chapter(),
        language="zh",
        budget_chars=8000,
        **_services(kb),
    )
    # 章预算 8000 ≈ 整书 24k 的三分之一：KB 段上限也摊成三分之一（≈2666 字）
    assert 2000 < len(digest.text) <= 8000


async def test_chapter_material_keeps_other_sources():
    notebook = Notebook.new(name="错题本")
    record = NotebookRecord.new(
        notebook_id=notebook.id, type="note", title="笔记", content_md="记录内容：频域。"
    )
    digest = await _collect_chapter(
        {"notebooks": [notebook.id]},
        notebook_service=FakeNotebooks([notebook], {notebook.id: [record]}),
    )
    assert "记录内容：频域。" in digest.text
