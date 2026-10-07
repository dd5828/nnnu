"""编译引擎（§7.14）：脚本化金路径、暂停/续跑、recover、整章重跑、坏块与忙态。

LLM 全走 ScriptedLLM：步骤按「章节序 × 页内块序」串行消费——脚本顺序必须和
spine 的 blocks_plan 完全一致（脚本耗尽抛 RuntimeError，正是防错位假绿）。
KB 素材走真实链路（NNNU_EMBEDDING_MOCK=scripted），app_client 与 test_book_api 同款；
animation 用例另起一个设了 NNNU_MANIM_MOCK=1 的 app（env 必须在 lifespan 前设好）。
"""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.render.validation import (
    extract_artifact_json,
    extract_render_code,
    validate_visualization,
)

pytestmark = pytest.mark.usefixtures("repo_prompts")

DOCS = {
    "第一章.md": "# 第一章\n\n傅里叶变换把信号拆成频率来看。\n",
    "第二章.md": "# 第二章\n\n频谱图上横轴是频率，纵轴是振幅。\n",
    "第三章.md": "# 第三章\n\n动手把两个正弦波叠起来看。\n",
}
USAGE = {"prompt_tokens": 120, "completion_tokens": 30}

TEXT1 = "## 开场\n\n傅里叶变换把信号拆成频率看。"
QUIZ = json.dumps(
    {
        "stem": "频谱图的横轴是什么？",
        "options": [{"key": "A", "text": "频率"}, {"key": "B", "text": "时间"}],
        "answer_key": ["A"],
        "explanation": "横轴是频率，纵轴是振幅。",
    },
    ensure_ascii=False,
)
FLASH = json.dumps(
    {"cards": [{"front": "傅里叶变换做什么？", "back": "把信号拆成频率成分"}]},
    ensure_ascii=False,
)
FLASH2 = json.dumps(
    {"cards": [{"front": "频谱横轴看什么？", "back": "频率"}]},
    ensure_ascii=False,
)
FIGURE = "频谱示意\n\n```mermaid\ngraph TD\n  A[时域信号] --> B[频域分解]\n```\n"
FIGURE2 = "两次分解\n\n```mermaid\ngraph TD\n  A[信号] --> B[拆出频率]\n```\n"
TEXT2 = "## 收尾\n\n动手把两个正弦波叠起来看。"

ANALYSIS = json.dumps(
    {
        "learning_goal": "演示方波由正弦波叠加逼近",
        "math_focus": ["傅里叶级数"],
        "visual_targets": ["方波"],
        "narrative_steps": ["画方波", "叠加"],
    },
    ensure_ascii=False,
)
DESIGN = json.dumps(
    {
        "title": "傅里叶级数演示",
        "scene_outline": ["画方波"],
        "visual_style": "深色背景",
        "animation_notes": ["每步停顿"],
        "code_constraints": ["对象不重叠"],
    },
    ensure_ascii=False,
)
ANIM_CODE = "```python\nfrom manim import *\n\n\nclass FourierScene(Scene):\n    def construct(self):\n        self.wait(2)\n```"
ANIM_SUMMARY = json.dumps(
    {"summary_text": "把方波的傅里叶逼近做成了动画。", "key_points": ["方波"]},
    ensure_ascii=False,
)


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


@pytest.fixture
async def manim_app_client(tmp_home, monkeypatch):
    """假渲染器（NNNU_MANIM_MOCK=1）+ 临时 home：animation 块走替身流水线。"""
    monkeypatch.setenv("NNNU_EMBEDDING_MOCK", "scripted")
    monkeypatch.setenv("NNNU_MANIM_MOCK", "1")
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


def _spine_json(ref: str, *, third: dict | None = None) -> str:
    """三章齐全、三件套齐；source_refs 引用真实 KB 文档。"""
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
            third or _chapter("ch-3", "动手试试", [{"type": "text", "focus": "收尾"}], ref),
        ],
        "concept_graph": {
            "nodes": [{"id": "fourier", "label": "傅里叶变换", "group": "基础"}],
            "edges": [],
        },
    }
    return json.dumps(payload, ensure_ascii=False)


async def _make_kb(app) -> tuple[str, str]:
    """建一个含 3 份文档的现成 KB；返回 (kb_id, 第一份文档的 doc_id)。"""
    kb = app.state.kb
    manifest = await kb.create_kb("傅里叶教材")
    for filename, text in DOCS.items():
        await kb.add_doc(manifest.id, filename, text.encode("utf-8"), "text/markdown")
    await kb.wait_idle(manifest.id, timeout=60)
    manifest = kb.get_kb(manifest.id)
    assert manifest is not None
    return manifest.id, manifest.docs[0].doc_id


def _install_spine(ref: str, *, third: dict | None = None) -> ScriptedLLM:
    scripted = ScriptedLLM([ScriptedStep(chunks=[_spine_json(ref, third=third)], usage=USAGE)])
    install_scripted(lambda: scripted)
    return scripted


def _install_compile_script(*texts: str, delays: dict[int, int] | None = None) -> ScriptedLLM:
    """装编译期的 LLM 脚本（换掉建书那一步的注入）；steps 顺序 = 块生成顺序。"""
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[text], usage=USAGE, delay_ms=(delays or {}).get(index, 0))
            for index, text in enumerate(texts)
        ]
    )
    install_scripted(lambda: scripted)
    return scripted


async def _create_book(client, kb_id: str, *, title: str = "傅里叶入门") -> dict:
    response = await client.post(
        "/api/v1/books", json={"title": title, "sources": {"kbs": [kb_id]}, "language": "zh"}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _pages_of(app, book_id: str) -> list[list[dict]]:
    """按页序读出整页块数组（编译的真落点），直接查库不借私有 API。"""
    rows = await app.state.db.fetch_all(
        "SELECT blocks FROM book_pages WHERE book_id = ? ORDER BY page_no, rowid", (book_id,)
    )
    return [json.loads(row["blocks"]) for row in rows]


async def _page_row(app, book_id: str, chapter_key: str) -> dict:
    row = await app.state.db.fetch_one(
        "SELECT * FROM book_pages WHERE book_id = ? AND chapter_key = ?", (book_id, chapter_key)
    )
    assert row is not None
    return row


async def _usage_total(app, book_id: str) -> int:
    rows = await app.state.db.fetch_all(
        "SELECT * FROM usage_records WHERE turn_id = ?", (f"book-compile-{book_id}",)
    )
    return sum(int(row["input_tokens"]) for row in rows)


async def test_compile_golden_path(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)
    scripted = _install_compile_script(TEXT1, QUIZ, FLASH, FIGURE, TEXT2)

    started = await client.post(f"/api/v1/books/{book['id']}/compile")
    assert started.status_code == 202
    assert started.json() == {"started": True}
    await app.state.book.wait_compile_idle(book["id"], timeout=30)

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "ready"
    assert [page["block_count"] for page in detail["pages"]] == [2, 2, 1]
    assert all(page["blocks_done"] == page["block_count"] for page in detail["pages"])

    # 五步各就各位：章节序 × 块序，每块恰好一次调用
    assert scripted.exhausted and len(scripted.calls) == 5
    pages = await _pages_of(app, book["id"])
    assert [[block["type"] for block in page] for page in pages] == [
        ["text", "quiz"],
        ["flashcard", "figure"],
        ["text"],
    ]
    assert all(block["status"] == "done" for page in pages for block in page)
    assert pages[0][0]["payload"]["markdown"] == TEXT1
    quiz = pages[0][1]["payload"]
    assert quiz["stem"] == "频谱图的横轴是什么？" and quiz["answer_key"] == ["A"]
    assert pages[1][0]["payload"]["cards"][0]["front"] == "傅里叶变换做什么？"
    # figure 的 mermaid 真过确定性校验器（编译期的深校验门）
    figure_md = pages[1][1]["payload"]["markdown"]
    render_code = extract_render_code(figure_md, "mermaid")
    assert render_code is not None
    ok, reason = validate_visualization(render_code, "mermaid")
    assert ok, reason
    assert pages[2][0]["payload"]["markdown"] == TEXT2
    # 块继承章节来源引用（追溯出处）
    assert pages[0][0]["source_refs"][0]["ref"] == doc_id

    # 记账：整次编译一笔合成 turn（5 步用量合计）
    assert await _usage_total(app, book["id"]) == 5 * 120


async def test_pause_then_resume_keeps_done_blocks(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)
    # 第一步慢 500ms：暂停请求落在第一块生成中间，块边界上收手
    scripted = _install_compile_script(TEXT1, QUIZ, FLASH, FIGURE, TEXT2, delays={0: 500})

    assert (await client.post(f"/api/v1/books/{book['id']}/compile")).status_code == 202
    # 等第一块的 LLM 调用真的在路上（step1 慢 500ms），此刻发暂停才是「生成中途」
    for _ in range(200):
        if scripted.calls:
            break
        await asyncio.sleep(0.01)
    assert scripted.calls, "编译任务没按预期开跑"
    paused = await client.post(f"/api/v1/books/{book['id']}/compile/pause")
    assert paused.status_code == 200 and paused.json() == {"paused": True}
    await app.state.book.wait_compile_idle(book["id"], timeout=30)

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "paused"
    # 块边界收手：在途的第一块编完了，后面的块一块没动
    assert sum(page["blocks_done"] for page in detail["pages"]) == 1
    assert any(page["block_count"] > page["blocks_done"] for page in detail["pages"])
    before = [block for page in await _pages_of(app, book["id"]) for block in page]
    assert len(scripted.calls) == 1

    resumed = await client.post(f"/api/v1/books/{book['id']}/compile")
    assert resumed.status_code == 202
    await app.state.book.wait_compile_idle(book["id"], timeout=30)

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "ready"
    assert all(page["blocks_done"] == page["block_count"] for page in detail["pages"])
    # 续跑跳过已完成块：每块恰好一次调用，不重生成
    assert scripted.exhausted and len(scripted.calls) == 5
    after = {block["id"]: block for page in await _pages_of(app, book["id"]) for block in page}
    for block in before:
        if block["status"] == "done":
            assert after[block["id"]]["status"] == "done"
            assert after[block["id"]]["payload"] == block["payload"]


async def test_recover_compiling_book_back_to_paused_then_resume(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)

    # 模拟进程被杀：书卡在 compiling，页里一个块卡在 compiling、一个已完成
    page = await _page_row(app, book["id"], "ch-1")
    stuck = [
        {"id": "blk-old-done", "type": "text", "status": "done", "payload": {"markdown": "旧文"}},
        {"id": "blk-old-stuck", "type": "quiz", "status": "compiling", "payload": {}},
    ]
    await app.state.db.execute(
        "UPDATE book_pages SET blocks = ? WHERE id = ?",
        (json.dumps(stuck, ensure_ascii=False), page["id"]),
    )
    await app.state.db.execute("UPDATE books SET status = 'compiling' WHERE id = ?", (book["id"],))

    await app.state.book.recover_compile()

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "paused"
    assert detail["pages"][0]["blocks_done"] == 1
    assert detail["pages"][0]["blocks_error"] == 0
    recovered = json.loads((await _page_row(app, book["id"], "ch-1"))["blocks"])
    assert recovered[0]["status"] == "done"  # 完成的块没被动
    assert recovered[1]["status"] == "pending"  # 卡住的退回待生成

    # 恢复后接着编完：脚本只要补 ch-1 剩下的 quiz + ch-2/ch-3
    scripted = _install_compile_script(QUIZ, FLASH, FIGURE, TEXT2)
    assert (await client.post(f"/api/v1/books/{book['id']}/compile")).status_code == 202
    await app.state.book.wait_compile_idle(book["id"], timeout=30)

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "ready"
    assert scripted.exhausted
    pages = await _pages_of(app, book["id"])
    assert [block["id"] for block in pages[0]] == ["blk-old-done", "blk-old-stuck"]  # 页没重建
    assert all(block["status"] == "done" for page in pages for block in page)


async def test_chapter_rerun_rebuilds_blocks_and_clears_attempts(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)
    scripted = _install_compile_script(TEXT1, QUIZ, FLASH, FIGURE, TEXT2)
    assert (await client.post(f"/api/v1/books/{book['id']}/compile")).status_code == 202
    await app.state.book.wait_compile_idle(book["id"], timeout=30)
    assert scripted.exhausted

    page = await _page_row(app, book["id"], "ch-2")
    before = json.loads(page["blocks"])
    figure_block = next(block for block in before if block["type"] == "figure")
    await app.state.db.execute(
        "INSERT INTO book_attempts (id, book_id, page_id, block_id, answer, correct, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bat-t1", book["id"], page["id"], figure_block["id"], '["A"]', 1, time.time()),
    )
    ch1_ids = [block["id"] for block in (await _pages_of(app, book["id"]))[0]]

    # 换一套新产物整章重跑 ch-2
    scripted2 = _install_compile_script(FLASH2, FIGURE2)
    started = await client.post(f"/api/v1/books/{book['id']}/compile", json={"chapters": ["ch-2"]})
    assert started.status_code == 202
    await app.state.book.wait_compile_idle(book["id"], timeout=30)

    assert scripted2.exhausted and len(scripted2.calls) == 2
    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "ready"
    after = json.loads((await _page_row(app, book["id"], "ch-2"))["blocks"])
    assert [block["type"] for block in after] == ["flashcard", "figure"]
    assert all(block["status"] == "done" for block in after)
    # 块 id 整批换新、payload 是新产物
    assert {block["id"] for block in after}.isdisjoint({block["id"] for block in before})
    assert after[0]["payload"]["cards"][0]["front"] == "频谱横轴看什么？"
    # 旧作答跟着清掉（块 id 都对不上了）
    left = await app.state.db.fetch_one(
        "SELECT COUNT(*) AS n FROM book_attempts WHERE page_id = ?", (page["id"],)
    )
    assert int(left["n"]) == 0
    # 没点到的章节原样不动
    assert [block["id"] for block in (await _pages_of(app, book["id"]))[0]] == ch1_ids


async def test_bad_block_marks_error_and_book_still_compiles(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)
    # quiz 两步都是散文 → 两次过后记 error；其余块照编
    scripted = _install_compile_script(TEXT1, "不是 JSON", "还是散文", FLASH, FIGURE, TEXT2)

    assert (await client.post(f"/api/v1/books/{book['id']}/compile")).status_code == 202
    await app.state.book.wait_compile_idle(book["id"], timeout=30)

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "ready"  # 坏块不炸整书
    pages = await _pages_of(app, book["id"])
    assert pages[0][1]["status"] == "error"
    assert "两次都没生成成功" in pages[0][1]["error"]
    assert pages[0][0]["status"] == "done" and pages[2][0]["status"] == "done"
    assert scripted.exhausted and len(scripted.calls) == 6


async def test_compile_busy_and_pause_noop_and_errors(app_client):
    app, client = app_client
    kb_id, doc_id = await _make_kb(app)
    _install_spine(doc_id)
    book = await _create_book(client, kb_id)
    _install_compile_script(TEXT1, QUIZ, FLASH, FIGURE, TEXT2, delays={0: 800})

    compile_url = f"/api/v1/books/{book['id']}/compile"
    assert (await client.post(compile_url)).status_code == 202
    busy = await client.post(compile_url)
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "compile_busy"
    assert busy.json()["error"]["recoverable"] is True

    pause_url = f"{compile_url}/pause"
    assert (await client.post(pause_url)).json() == {"paused": True}
    await app.state.book.wait_compile_idle(book["id"], timeout=30)
    noop = await client.post(pause_url)  # 没任务在跑：200 no-op
    assert noop.status_code == 200 and noop.json() == {"paused": False}

    assert (await client.post("/api/v1/books/bk-00000000/compile")).status_code == 404
    assert (await client.post("/api/v1/books/bk-00000000/compile/pause")).status_code == 404
    unknown = await client.post(compile_url, json={"chapters": ["ch-9"]})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_chapter"
    empty = await client.post(compile_url, json={"chapters": []})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_request"


async def test_animation_block_goes_through_render_pipeline(manim_app_client):
    app, client = manim_app_client
    kb_id, doc_id = await _make_kb(app)
    third = _chapter("ch-3", "看个动画", [{"type": "animation", "focus": "方波叠加"}], doc_id)
    _install_spine(doc_id, third=third)
    book = await _create_book(client, kb_id)
    # text/quiz/flashcard/figure 各一站；animation 走六阶段里的 4 次 LLM 调用
    scripted = _install_compile_script(
        TEXT1, QUIZ, FLASH, FIGURE, ANALYSIS, DESIGN, ANIM_CODE, ANIM_SUMMARY
    )

    assert (await client.post(f"/api/v1/books/{book['id']}/compile")).status_code == 202
    await app.state.book.wait_compile_idle(book["id"], timeout=60)

    detail = (await client.get(f"/api/v1/books/{book['id']}")).json()
    assert detail["status"] == "ready"
    assert scripted.exhausted and len(scripted.calls) == 8
    pages = await _pages_of(app, book["id"])
    animation = pages[2][0]
    assert animation["type"] == "animation" and animation["status"] == "done"
    body = animation["payload"]["markdown"]
    assert "```nnnu-artifact" in body and ANIM_CODE in body
    artifact = extract_artifact_json(body)
    assert artifact is not None
    assert artifact["url"].startswith("/api/v1/renders/rnd-")
    # 动画那 4 次调用也记进了编译这一笔账
    assert await _usage_total(app, book["id"]) == 8 * 120
