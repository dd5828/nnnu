"""Book REST（§7.14）：建书（同步跑 spine）、目录编辑、删除、错误信封、记账、失败清理。

KB 素材走真实链路（NNNU_EMBEDDING_MOCK=scripted 离线嵌入），spine 输出走
ScriptedLLM；app_client 同 conftest.client（跑 lifespan）但把 app 交出来——
要拿 app.state.kb 建库、app.state.db 查记账行。
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

pytestmark = pytest.mark.usefixtures("repo_prompts")

DOC_MD = "# 第一章\n\n傅里叶变换把信号拆成频率来看。\n"


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture
async def app_client(tmp_home, monkeypatch):
    monkeypatch.setenv("NNNU_EMBEDDING_MOCK", "scripted")
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c


def _chapter(key: str, title: str, blocks: list[dict], ref: str) -> dict:
    return {
        "key": key,
        "title": title,
        "summary": f"{title}的摘要",
        "objectives": ["知道傅里叶在干什么"],
        "content_type": "theory",
        "blocks_plan": blocks,
        "source_refs": [{"kind": "kb", "ref": ref, "label": "傅里叶教材·第一章.md"}],
    }


def _spine_json(ref: str) -> str:
    """三章齐全、三件套齐；source_refs 引用真实 KB 文档（ref 不在素材清单会被收编重试）。"""
    payload = {
        "chapters": [
            _chapter(
                "ch-1",
                "什么是傅里叶变换",
                [{"type": "text", "focus": "开场"}, {"type": "quiz", "focus": "小测"}],
                ref,
            ),
            _chapter(
                "ch-2",
                "频域的世界",
                [{"type": "flashcard", "focus": "背名词"}, {"type": "figure", "focus": "画频谱"}],
                ref,
            ),
            _chapter("ch-3", "动手试试", [{"type": "text", "focus": "收尾"}], ref),
        ],
        "concept_graph": {
            "nodes": [{"id": "fourier", "label": "傅里叶变换", "group": "基础"}],
            "edges": [],
        },
    }
    return json.dumps(payload, ensure_ascii=False)


async def _make_kb(app) -> tuple[str, str]:
    """建一个只有一份文档的现成 KB；返回 (kb_id, doc_id)。"""
    kb = app.state.kb
    manifest = await kb.create_kb("傅里叶教材")
    await kb.add_doc(manifest.id, "第一章.md", DOC_MD.encode("utf-8"), "text/markdown")
    await kb.wait_idle(manifest.id, timeout=60)
    manifest = kb.get_kb(manifest.id)
    assert manifest is not None
    return manifest.id, manifest.docs[0].doc_id


def _install_spine(ref: str, *, text: str | None = None, usage: dict | None = None) -> ScriptedLLM:
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[text or _spine_json(ref)],
                usage=usage or {"prompt_tokens": 800, "completion_tokens": 200},
            )
        ]
    )
    install_scripted(lambda: scripted)
    return scripted


async def _create_book(client, kb_id: str, *, language: str = "zh", title: str = "傅里叶入门"):
    response = await client.post(
        "/api/v1/books", json={"title": title, "sources": {"kbs": [kb_id]}, "language": language}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_book_round_trip(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    scripted = _install_spine(doc_id)

    book = await _create_book(client, kb_id)
    assert book["id"].startswith("bk-")
    assert book["status"] == "draft"
    assert [chapter["key"] for chapter in book["spine"]["chapters"]] == ["ch-1", "ch-2", "ch-3"]
    # spine 的 source_refs 收编成素材清单里的真实 ref（KB 文档 id）与清单里的 label
    doc_ref = book["spine"]["chapters"][0]["source_refs"]
    assert doc_ref == [{"kind": "kb", "ref": doc_id, "label": "傅里叶教材·第一章.md"}]
    assert book["issues"] == []
    assert book["estimate"]["totals"]["tokens"] > 0
    assert len(book["pages"]) == 3
    assert all(page["block_count"] == 0 and not page["visited"] for page in book["pages"])
    # 建书走的提示词是中文版
    assert "nnnu 活书引擎" in scripted.calls[0].messages[0]["content"]

    listed = (await client.get("/api/v1/books")).json()["books"]
    assert [item["id"] for item in listed] == [book["id"]]
    assert listed[0]["chapter_count"] == 3

    # 目录编辑：改名（去空白）、删 ch-3、把 ch-2 排到最前 → 页跟着对齐、块计划保住了
    patched = await client.patch(
        f"/api/v1/books/{book['id']}",
        json={
            "chapters": [
                {"key": "ch-2", "title": "  换个名字  "},
                {"key": "ch-1", "title": "什么是傅里叶变换"},
            ]
        },
    )
    assert patched.status_code == 200, patched.text
    detail = patched.json()
    assert [(chapter["key"], chapter["title"]) for chapter in detail["spine"]["chapters"]] == [
        ("ch-2", "换个名字"),
        ("ch-1", "什么是傅里叶变换"),
    ]
    assert [(page["chapter_key"], page["page_no"]) for page in detail["pages"]] == [
        ("ch-2", 1),
        ("ch-1", 2),
    ]
    assert detail["spine"]["chapters"][0]["blocks_plan"]

    # 改书名（章节不动）
    renamed = (await client.patch(f"/api/v1/books/{book['id']}", json={"title": "新书名"})).json()
    assert renamed["title"] == "新书名"
    assert len(renamed["spine"]["chapters"]) == 2

    # 记账：spine 生成挂合成 turn_id
    row = await app.state.db.fetch_one(
        "SELECT * FROM usage_records WHERE turn_id = ?", (f"book-spine-{book['id']}",)
    )
    assert row is not None
    assert row["input_tokens"] == 800 and row["output_tokens"] == 200

    # 删书：页一起走
    assert (await client.delete(f"/api/v1/books/{book['id']}")).json() == {"deleted": book["id"]}
    assert (await client.get(f"/api/v1/books/{book['id']}")).status_code == 404
    assert (await client.get("/api/v1/books")).json()["books"] == []
    pages = await app.state.db.fetch_one("SELECT COUNT(*) AS n FROM book_pages")
    assert int(pages["n"]) == 0


async def test_create_validation_errors_and_cleanup(app_client):
    _, client = app_client
    empty_sources = await client.post("/api/v1/books", json={"title": "x", "sources": {}})
    assert empty_sources.status_code == 422
    assert empty_sources.json()["error"]["code"] == "invalid_sources"

    blank_title = await client.post(
        "/api/v1/books", json={"title": "  ", "sources": {"kbs": ["kb-1"]}}
    )
    assert blank_title.status_code == 422
    assert blank_title.json()["error"]["code"] == "invalid_title"

    # 素材一份都用不上（库不存在）：422，且不留孤儿书
    missing = await client.post(
        "/api/v1/books", json={"title": "x", "sources": {"kbs": ["kb-404"]}}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "sources_unavailable"
    assert missing.json()["error"]["recoverable"] is True
    assert (await client.get("/api/v1/books")).json()["books"] == []

    assert (await client.get("/api/v1/books/bk-00000000")).status_code == 404
    assert (await client.delete("/api/v1/books/bk-00000000")).status_code == 404


async def test_create_without_model_503_and_cleanup(app_client, monkeypatch):
    app, client = app_client
    kb_id, _doc_id = await _make_kb(app)
    import nnnu.book.llm as llm_module
    from nnnu.services.llm.errors import LLMConfigError

    def _no_model():
        raise LLMConfigError("没有配置模型")

    monkeypatch.setattr(llm_module, "resolve_model_config", _no_model)
    response = await client.post("/api/v1/books", json={"title": "x", "sources": {"kbs": [kb_id]}})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert (await client.get("/api/v1/books")).json()["books"] == []


async def test_bad_spine_output_502_and_cleanup(app_client):
    app, client = app_client
    kb_id, _doc_id = await _make_kb(app)
    scripted = ScriptedLLM(
        [ScriptedStep(chunks=["这不是 JSON"]), ScriptedStep(chunks=["还不是 JSON"])]
    )
    install_scripted(lambda: scripted)
    response = await client.post("/api/v1/books", json={"title": "x", "sources": {"kbs": [kb_id]}})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_output_invalid"
    assert len(scripted.calls) == 2  # 修复重试过一次
    assert (await client.get("/api/v1/books")).json()["books"] == []


async def test_spine_issues_surface_and_chapters_locked_after_draft(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    # 这份 spine 缺 flashcard → 软问题：重试一次后收下，GET 里带警示
    bad = json.loads(_spine_json(doc_id))
    bad["chapters"][1]["blocks_plan"] = [{"type": "figure", "focus": "画频谱"}]
    text = json.dumps(bad, ensure_ascii=False)
    scripted = ScriptedLLM([ScriptedStep(chunks=[text]), ScriptedStep(chunks=[text])])
    install_scripted(lambda: scripted)
    book = await _create_book(client, kb_id)
    assert any("flashcard" in issue for issue in book["issues"])

    # 状态离开 draft（模拟编译中）→ 目录锁定 409，删书 409，改名仍然可以
    await app.state.db.execute("UPDATE books SET status = 'compiling' WHERE id = ?", (book["id"],))
    busy = await client.patch(
        f"/api/v1/books/{book['id']}", json={"chapters": [{"key": "ch-1", "title": "x"}]}
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "not_draft"
    deleted = await client.delete(f"/api/v1/books/{book['id']}")
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "compile_busy"
    renamed = await client.patch(f"/api/v1/books/{book['id']}", json={"title": "编译中也想改名"})
    assert renamed.status_code == 200


async def test_patch_chapter_errors(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)
    url = f"/api/v1/books/{book['id']}"

    unknown = await client.patch(url, json={"chapters": [{"key": "ch-9", "title": "x"}]})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_chapter"

    empty = await client.patch(url, json={"chapters": []})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_spine"

    blank = await client.patch(url, json={"chapters": [{"key": "ch-1", "title": "   "}]})
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "invalid_title"

    nothing = await client.patch(url, json={})
    assert nothing.status_code == 422
    assert nothing.json()["error"]["code"] == "invalid_request"


async def test_create_in_english_uses_english_prompts(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    scripted = _install_spine(doc_id)
    book = await _create_book(client, kb_id, language="en")
    assert book["spine"]["language"] == "en"
    assert "You design the table of contents" in scripted.calls[0].messages[0]["content"]
