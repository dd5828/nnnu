"""源指纹快照与漂移（§7.14 健康检查）：编译时拍快照、看书时三态比对。

假服务只搭各 service 被用到的那几个方法（KB 的 get_kb 是同步的，照真实签名）。
覆盖：干净回 ok、KB 文档 changed/added/missing、整库没了全 missing、删除状态文档
算 missing、旧文档无指纹时退化成「文件名+大小」、笔记本/题库的增改、会话只记不告警。
"""

from nnnu.book.health import check_drift, take_snapshot
from nnnu.services.knowledge.types import DOC_DELETED, KbDoc, KbManifest
from nnnu.services.notebooks.models import Notebook, NotebookRecord
from nnnu.services.question_bank.models import Question
from nnnu.services.sessions.models import Message, Session

SOURCES = {
    "kbs": ["kb-1"],
    "notebooks": ["nb-1"],
    "questions": {"filter": "ids", "ids": ["q-1"]},
    "sessions": ["sess-1"],
}


class FakeKbService:
    def __init__(self) -> None:
        self.kbs: dict[str, KbManifest] = {}

    def get_kb(self, kb_id: str) -> KbManifest | None:
        return self.kbs.get(kb_id)


class FakeNotebookService:
    def __init__(self) -> None:
        self.notebooks: dict[str, Notebook] = {}
        self.records: dict[str, list[NotebookRecord]] = {}

    async def get_notebook(self, notebook_id: str) -> Notebook | None:
        return self.notebooks.get(notebook_id)

    async def list_records(self, notebook_id: str) -> list[NotebookRecord]:
        return list(self.records.get(notebook_id, []))


class FakeQuestionService:
    def __init__(self) -> None:
        self.questions: dict[str, Question] = {}

    async def get_question(self, question_id: str) -> Question | None:
        return self.questions.get(question_id)

    async def list_questions(self, *, filter: str = "all", limit: int = 50) -> list[Question]:
        return list(self.questions.values())[:limit]


class FakeSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.messages: dict[str, list[Message]] = {}

    async def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def list_messages(self, session_id: str) -> list[Message]:
        return list(self.messages.get(session_id, []))


def _services():
    return FakeKbService(), FakeNotebookService(), FakeQuestionService(), FakeSessionManager()


def _doc(doc_id: str, filename: str, *, fingerprint: str = "sha256:aaa", size: int = 10) -> KbDoc:
    return KbDoc(doc_id=doc_id, filename=filename, size=size, fingerprint=fingerprint)


def _kb(*docs: KbDoc, kb_id: str = "kb-1", name: str = "教材库") -> KbManifest:
    return KbManifest(id=kb_id, name=name, status="ready", docs=list(docs))


def _record(record_id: str = "nbr-1", *, title: str = "频谱要点", content: str = "横轴是频率。"):
    return NotebookRecord(
        id=record_id, notebook_id="nb-1", type="note", title=title, content_md=content
    )


def _question(question_id: str = "q-1", *, stem: str = "频谱横轴是什么？", answer: str = "A"):
    return Question(id=question_id, stem=stem, answer=answer, knowledge_point="频谱")


def _seed(services):
    kb_service, notebook_service, question_service, session_manager = services
    kb_service.kbs["kb-1"] = _kb(_doc("kbdoc-1", "第一章.md"))
    notebook_service.notebooks["nb-1"] = Notebook(id="nb-1", name="课堂笔记")
    notebook_service.records["nb-1"] = [_record()]
    question_service.questions["q-1"] = _question()
    session_manager.sessions["sess-1"] = Session(id="sess-1", title="答疑")
    session_manager.messages["sess-1"] = [
        Message(id="msg-1", session_id="sess-1", role="user", content="第一问"),
        Message(id="msg-2", session_id="sess-1", role="assistant", content="第一答"),
    ]


async def _snapshot(services, sources=SOURCES):
    kb_service, notebook_service, question_service, session_manager = services
    return await take_snapshot(
        sources,
        kb_service=kb_service,
        notebook_service=notebook_service,
        question_service=question_service,
        session_manager=session_manager,
    )


async def _check(services, snapshot, sources=SOURCES):
    kb_service, notebook_service, question_service, _ = services
    return await check_drift(
        snapshot,
        sources,
        kb_service=kb_service,
        notebook_service=notebook_service,
        question_service=question_service,
    )


async def test_snapshot_shape_and_clean_check():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    assert snapshot["kbs"]["kb-1"]["docs"]["kbdoc-1"] == {
        "fp": "sha256:aaa",
        "filename": "第一章.md",
    }
    assert snapshot["notebooks"]["nb-1"]["records"]["nbr-1"]["title"] == "频谱要点"
    assert snapshot["questions"]["q-1"]["label"] == "频谱"
    assert snapshot["sessions"] == {"sess-1": 2}

    report = await _check(services, snapshot)
    assert report == {
        "status": "ok",
        "drift": [],
        "counts": {"added": 0, "changed": 0, "missing": 0},
    }


async def test_kb_doc_changed_added_and_missing():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    # 快照后：第一章改了内容、多了第二章、第三章整份没了
    snapshot["kbs"]["kb-1"]["docs"]["kbdoc-1"]["fp"] = "sha256:old"
    snapshot["kbs"]["kb-1"]["docs"]["kbdoc-3"] = {"fp": "sha256:ccc", "filename": "第三章.md"}
    services[0].kbs["kb-1"] = _kb(
        _doc("kbdoc-1", "第一章.md", fingerprint="sha256:bbb"),
        _doc("kbdoc-2", "第二章.md"),
    )

    report = await _check(services, snapshot)
    assert report["status"] == "drift"
    assert report["counts"] == {"added": 1, "changed": 1, "missing": 1}
    by_change = {item["change"]: item for item in report["drift"]}
    assert by_change["changed"]["ref"] == "kbdoc-1"
    assert by_change["changed"]["label"] == "教材库·第一章.md"
    assert by_change["added"]["ref"] == "kbdoc-2"
    assert by_change["missing"]["ref"] == "kbdoc-3"


async def test_removed_kb_makes_all_snapshot_docs_missing():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    services[0].kbs.clear()  # 整个库被删了

    report = await _check(services, snapshot)
    assert [item["change"] for item in report["drift"]] == ["missing"]
    assert report["drift"][0]["label"] == "教材库·第一章.md"


async def test_deleted_status_doc_counts_as_missing():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    gone = _doc("kbdoc-1", "第一章.md")
    gone.status = DOC_DELETED  # live_docs 不含它 → 快照里的这条算没了
    services[0].kbs["kb-1"] = _kb(gone)

    report = await _check(services, snapshot)
    assert report["counts"]["missing"] == 1 and report["status"] == "drift"


async def test_doc_without_fingerprint_falls_back_to_filename_and_size():
    services = _services()
    services[0].kbs["kb-1"] = _kb(_doc("kbdoc-1", "第一章.md", fingerprint="", size=10))
    sources = {**SOURCES, "notebooks": [], "questions": {}, "sessions": []}
    snapshot = await _snapshot(services, sources)
    assert snapshot["kbs"]["kb-1"]["docs"]["kbdoc-1"]["fp"] == "第一章.md:10"

    # 只改大小也算漂移（老数据没有 sha256，退化成文件名+大小照样比得出来）
    services[0].kbs["kb-1"] = _kb(_doc("kbdoc-1", "第一章.md", fingerprint="", size=99))
    report = await _check(services, snapshot, sources)
    assert report["counts"]["changed"] == 1
    assert report["drift"][0]["change"] == "changed"


async def test_notebook_record_changed_and_added():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    services[1].records["nb-1"] = [
        _record(content="横轴是频率，纵轴是振幅。"),  # 同一条记录改了内容
        _record("nbr-2", title="新增笔记"),  # 快照后新记的
    ]

    report = await _check(services, snapshot)
    changes = {(item["ref"], item["change"]) for item in report["drift"]}
    assert changes == {("nbr-1", "changed"), ("nbr-2", "added")}
    assert report["counts"] == {"added": 1, "changed": 1, "missing": 0}


async def test_question_changed_and_missing():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    services[2].questions["q-1"] = _question(answer="B")  # 同题改了答案

    report = await _check(services, snapshot)
    assert [(item["ref"], item["change"]) for item in report["drift"]] == [("q-1", "changed")]
    services[2].questions.clear()  # 题目被删了
    report = await _check(services, snapshot)
    assert [(item["ref"], item["change"]) for item in report["drift"]] == [("q-1", "missing")]


async def test_question_added_shows_up_in_list_window():
    # ids 口径只比对当时选中的几道；all/wrong 走列表窗口，窗口里多出来的才算 added
    services = _services()
    _seed(services)
    sources = {**SOURCES, "notebooks": [], "sessions": [], "questions": {"filter": "all"}}
    snapshot = await _snapshot(services, sources)
    services[2].questions["q-2"] = _question("q-2", stem="纵轴是什么？")
    # ids 口径（只认快照点选中的 q-1）：窗口外的 q-2 不报
    report = await _check(services, snapshot, SOURCES)
    assert report["status"] == "ok"
    report = await _check(services, snapshot, sources)
    assert [(item["ref"], item["change"]) for item in report["drift"]] == [("q-2", "added")]


async def test_session_messages_never_signal_drift():
    services = _services()
    _seed(services)
    snapshot = await _snapshot(services)
    # 会话多了新消息、甚至整个会话没了：都不告警（追加消息是常态）
    services[3].messages["sess-1"].append(
        Message(id="msg-3", session_id="sess-1", role="user", content="第二问")
    )
    assert (await _check(services, snapshot))["status"] == "ok"
    services[3].sessions.clear()
    assert (await _check(services, snapshot))["status"] == "ok"


async def test_snapshot_skips_sources_that_are_gone():
    services = _services()
    # 什么都没建：快照是空壳，不该抛
    snapshot = await _snapshot(services)
    assert snapshot == {"kbs": {}, "notebooks": {}, "questions": {}, "sessions": {}}
    assert (await _check(services, snapshot))["status"] == "ok"
