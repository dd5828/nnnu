"""源指纹快照与漂移检查（§7.14 健康检查）：编译时拍快照，看书时比对。

- KB：manifest 里每份文档的 fingerprint（§7.9 生成 sha256 时就注明给 Book 漂移复用）；
  旧文档没有指纹就退化成「文件名+大小」，两边同一算法，照样能比；
- 笔记本/题库：按「对象 id + 内容 sha」逐条比——内容改了报 changed，对象增删报 added/missing；
- 会话：只记消息数、**不参与告警**（追加消息是常态，报起来横幅永远亮）；
- 三种变化（added/changed/missing）一律叫漂移：源集合变大也算——书没覆盖新素材。

快照是编译开始时 best-effort 拍的（拍不动就当没有，不能让健康检查拖垮编译）；
比对在 GET 书详情时现算（只对 ready 的书算，编译中轮询不做无用功）。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from nnnu.book.inputs import QUESTION_COUNT

if TYPE_CHECKING:
    from nnnu.services.knowledge.service import KBService
    from nnnu.services.knowledge.types import KbDoc
    from nnnu.services.notebooks.service import NotebookService
    from nnnu.services.question_bank.service import QuestionBankService
    from nnnu.services.sessions.service import SessionManager

CHANGES = ("added", "changed", "missing")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _doc_fp(doc: "KbDoc") -> str:
    """文档指纹：优先 manifest 里的 sha256，老数据退化成「文件名+大小」。"""
    return doc.fingerprint or f"{doc.filename}:{doc.size}"


async def _selected_questions(
    question_service: "QuestionBankService", questions: dict[str, Any] | None
) -> list[Any]:
    """与 inputs.collect_materials 同一选择口径（ids 按 id 取；all/wrong 走列表窗口）。"""
    if not questions:
        return []
    filt = questions.get("filter", "all")
    if filt == "ids":
        items = []
        for question_id in list(questions.get("ids") or [])[:QUESTION_COUNT]:
            question = await question_service.get_question(question_id)
            if question is not None:
                items.append(question)
        return items
    return await question_service.list_questions(filter=filt, limit=QUESTION_COUNT)


async def take_snapshot(
    sources: dict[str, Any],
    *,
    kb_service: "KBService",
    notebook_service: "NotebookService",
    question_service: "QuestionBankService",
    session_manager: "SessionManager",
) -> dict[str, Any]:
    """编译时拍源快照（写进 books.fingerprints）；形状：

    {"kbs": {kb_id: {"name", "docs": {doc_id: {"fp", "filename"}}}},
     "notebooks": {nb_id: {"name", "records": {record_id: {"sha", "title"}}}},
     "questions": {q_id: {"sha", "label"}},
     "sessions": {sess_id: 消息数}}
    """
    kbs: dict[str, Any] = {}
    for kb_id in sources.get("kbs") or []:
        manifest = kb_service.get_kb(kb_id)
        if manifest is None:
            continue
        kbs[kb_id] = {
            "name": manifest.name,
            "docs": {
                doc.doc_id: {"fp": _doc_fp(doc), "filename": doc.filename}
                for doc in manifest.live_docs()
            },
        }
    notebooks: dict[str, Any] = {}
    for notebook_id in sources.get("notebooks") or []:
        notebook = await notebook_service.get_notebook(notebook_id)
        if notebook is None:
            continue
        records = await notebook_service.list_records(notebook_id)
        notebooks[notebook_id] = {
            "name": notebook.name,
            "records": {
                record.id: {"sha": _sha(record.content_md), "title": record.title}
                for record in records
            },
        }
    questions: dict[str, Any] = {}
    for question in await _selected_questions(question_service, sources.get("questions")):
        questions[question.id] = {
            "sha": _sha(f"{question.stem}\n{question.answer}"),
            "label": question.knowledge_point or question.stem[:40],
        }
    sessions: dict[str, int] = {}
    for session_id in sources.get("sessions") or []:
        session = await session_manager.get_session(session_id)
        if session is None:
            continue
        sessions[session_id] = len(await session_manager.list_messages(session_id))
    return {"kbs": kbs, "notebooks": notebooks, "questions": questions, "sessions": sessions}


async def check_drift(
    snapshot: dict[str, Any],
    sources: dict[str, Any],
    *,
    kb_service: "KBService",
    notebook_service: "NotebookService",
    question_service: "QuestionBankService",
) -> dict[str, Any]:
    """比对快照与现状；返回 {status, drift:[{kind, ref, label, change}], counts}。"""
    drift: list[dict[str, str]] = []

    for kb_id, snap in (snapshot.get("kbs") or {}).items():
        name = str(snap.get("name") or kb_id)
        manifest = kb_service.get_kb(kb_id)
        now: dict[str, "KbDoc"] = (
            {} if manifest is None else {doc.doc_id: doc for doc in manifest.live_docs()}
        )
        for doc_id, info in (snap.get("docs") or {}).items():
            label = f"{name}·{info.get('filename') or doc_id}"
            doc = now.get(doc_id)
            if doc is None:
                drift.append({"kind": "kb", "ref": doc_id, "label": label, "change": "missing"})
            elif _doc_fp(doc) != info.get("fp"):
                drift.append({"kind": "kb", "ref": doc_id, "label": label, "change": "changed"})
        for doc_id, doc in now.items():
            if doc_id not in (snap.get("docs") or {}):
                drift.append(
                    {
                        "kind": "kb",
                        "ref": doc_id,
                        "label": f"{name}·{doc.filename}",
                        "change": "added",
                    }
                )

    for notebook_id, snap in (snapshot.get("notebooks") or {}).items():
        name = str(snap.get("name") or notebook_id)
        notebook = await notebook_service.get_notebook(notebook_id)
        records = (
            {record.id: record for record in await notebook_service.list_records(notebook_id)}
            if notebook is not None
            else {}
        )
        for record_id, info in (snap.get("records") or {}).items():
            label = f"{name}·{info.get('title') or record_id}"
            record = records.get(record_id)
            if record is None:
                drift.append(
                    {"kind": "notebook", "ref": record_id, "label": label, "change": "missing"}
                )
            elif _sha(record.content_md) != info.get("sha"):
                drift.append(
                    {"kind": "notebook", "ref": record_id, "label": label, "change": "changed"}
                )
        for record_id, record in records.items():
            if record_id not in (snap.get("records") or {}):
                drift.append(
                    {
                        "kind": "notebook",
                        "ref": record_id,
                        "label": f"{name}·{record.title}",
                        "change": "added",
                    }
                )

    selected = {
        question.id: question
        for question in await _selected_questions(question_service, sources.get("questions"))
    }
    for question_id, info in (snapshot.get("questions") or {}).items():
        label = str(info.get("label") or question_id)
        question = selected.get(question_id)
        if question is None:
            drift.append(
                {"kind": "question", "ref": question_id, "label": label, "change": "missing"}
            )
        elif _sha(f"{question.stem}\n{question.answer}") != info.get("sha"):
            drift.append(
                {"kind": "question", "ref": question_id, "label": label, "change": "changed"}
            )
    for question_id, question in selected.items():
        if question_id not in (snapshot.get("questions") or {}):
            drift.append(
                {
                    "kind": "question",
                    "ref": question_id,
                    "label": question.knowledge_point or question.stem[:40],
                    "change": "added",
                }
            )

    # 会话只存快照不比对：追加消息是常态，告警会永远亮（见 STAGE_LOG 口径）
    counts = {name: sum(1 for item in drift if item["change"] == name) for name in CHANGES}
    return {
        "status": "drift" if drift else "ok",
        "drift": drift,
        "counts": counts,
    }
