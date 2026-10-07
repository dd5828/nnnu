"""块编辑 / 页聊天 / 作答 / 进度 / 导出 / 健康（§7.14 刀四）：httpx 全端点口径。

用例直插一本 ready 书 + 一页四块（编辑类不跑编译，省 LLM 步骤）；重生成/换类型与
页聊天用脚本化 LLM；animation 再生另起一个 NNNU_MANIM_MOCK=1 的 app（env 必须在
lifespan 前设好），202 后轮询页详情等块落地。
"""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.render.validation import extract_artifact_json

pytestmark = pytest.mark.usefixtures("repo_prompts")

USAGE = {"prompt_tokens": 120, "completion_tokens": 30}
TEXT2 = "## 改写\n\n新的正文。"
FLASH = json.dumps(
    {"cards": [{"front": "傅里叶变换做什么？", "back": "把信号拆成频率成分"}]},
    ensure_ascii=False,
)
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

TEXT_BLOCK = {
    "id": "blk-00000001",
    "type": "text",
    "status": "done",
    "focus": "开场",
    "payload": {"markdown": "## 开场\n\n傅里叶变换把信号拆成频率来看。"},
    "source_refs": [{"kind": "kb", "ref": "kbdoc-1", "label": "教材库·第1章.md"}],
}
QUIZ_BLOCK = {
    "id": "blk-00000002",
    "type": "quiz",
    "status": "done",
    "focus": "小测",
    "payload": {
        "stem": "频谱图的横轴是什么？",
        "options": [{"key": "A", "text": "频率"}, {"key": "B", "text": "时间"}],
        "answer_key": ["A"],
        "explanation": "横轴是频率，纵轴是振幅。",
    },
}
FIGURE_BLOCK = {
    "id": "blk-00000003",
    "type": "figure",
    "status": "done",
    "focus": "画频谱",
    "payload": {
        "markdown": "频谱示意\n\n```mermaid\ngraph TD\n  A[时域信号] --> B[频域分解]\n```\n"
    },
}
FLASH_BLOCK = {
    "id": "blk-00000004",
    "type": "flashcard",
    "status": "done",
    "focus": "背名词",
    "payload": {"cards": [{"front": "傅里叶变换做什么？", "back": "把信号拆成频率成分"}]},
}
ANIM_BLOCK = {
    "id": "blk-00000001",
    "type": "animation",
    "status": "done",
    "focus": "方波叠加",
    "payload": {"markdown": "旧的动画正文"},
}


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


def _spine_json() -> str:
    return json.dumps(
        {
            "chapters": [
                {
                    "key": "ch-1",
                    "title": "频域的世界",
                    "summary": "频谱怎么读",
                    "objectives": ["会看频谱图"],
                    "content_type": "theory",
                    "blocks_plan": [
                        {"type": "text", "focus": "开场"},
                        {"type": "quiz", "focus": "小测"},
                        {"type": "figure", "focus": "画频谱"},
                        {"type": "flashcard", "focus": "背名词"},
                    ],
                    "source_refs": [{"kind": "kb", "ref": "kbdoc-1", "label": "教材库·第1章.md"}],
                }
            ],
            "concept_graph": {
                "nodes": [{"id": "fourier", "label": "傅里叶变换", "group": "基础"}],
                "edges": [],
            },
            "language": "zh",
        },
        ensure_ascii=False,
    )


async def _seed(app, *, blocks=None, status="ready") -> tuple[str, str]:
    """直插一本 ready 书 + ch-1 一页（不跑编译）；返回 (book_id, page_id)。"""
    now = time.time()
    block_list = [
        dict(item) for item in (blocks or [TEXT_BLOCK, QUIZ_BLOCK, FIGURE_BLOCK, FLASH_BLOCK])
    ]
    await app.state.db.execute(
        "INSERT INTO books (id, title, sources, spine, status, fingerprints, error, "
        "created_at, updated_at) VALUES (?, ?, '{}', ?, ?, '{}', '', ?, ?)",
        ("bk-00000001", "傅里叶入门", _spine_json(), status, now, now),
    )
    await app.state.db.execute(
        "INSERT INTO book_pages (id, book_id, chapter_key, page_no, blocks, visited, "
        "bookmarked, updated_at) VALUES (?, ?, 'ch-1', 1, ?, 0, 0, ?)",
        ("bp-00000001", "bk-00000001", json.dumps(block_list, ensure_ascii=False), now),
    )
    return "bk-00000001", "bp-00000001"


def _blocks_url(book_id: str, page_id: str) -> str:
    return f"/api/v1/books/{book_id}/pages/{page_id}/blocks"


def _block_ids(payload: dict) -> list[str]:
    return [block["id"] for block in payload["blocks"]]


async def test_book_detail_and_page_flags_round_trip(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)

    detail = (await client.get(f"/api/v1/books/{book_id}")).json()
    assert detail["issues"] == []  # 直插的 spine 本身合规（后面断言的可信度基础）
    assert len(detail["pages"]) == 1 and detail["pages"][0]["block_count"] == 4
    # 看板口径的逐块摘要：三件（id/type/status），顺序即页内序
    assert detail["pages"][0]["blocks"] == [
        {"id": "blk-00000001", "type": "text", "status": "done"},
        {"id": "blk-00000002", "type": "quiz", "status": "done"},
        {"id": "blk-00000003", "type": "figure", "status": "done"},
        {"id": "blk-00000004", "type": "flashcard", "status": "done"},
    ]

    page = (await client.get(f"/api/v1/books/{book_id}/pages/{page_id}")).json()
    assert [block["type"] for block in page["blocks"]] == ["text", "quiz", "figure", "flashcard"]
    assert page["chapter"]["title"] == "频域的世界" and page["messages"] == []

    flags = await client.patch(
        f"/api/v1/books/{book_id}/pages/{page_id}", json={"visited": True, "bookmarked": True}
    )
    assert flags.json() == {"visited": True, "bookmarked": True}
    listed = (await client.get("/api/v1/books")).json()["books"]
    assert listed[0]["progress"]["visited"] == 1 and listed[0]["progress"]["bookmarked"] == 1

    empty = await client.patch(f"/api/v1/books/{book_id}/pages/{page_id}", json={})
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "invalid_request"
    gone = await client.get("/api/v1/books/bk-00000000/pages/bp-00000001")
    assert gone.status_code == 404


async def test_move_block_swaps_and_clamps_at_edges(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = _blocks_url(book_id, page_id)

    moved = await client.patch(
        url, json={"op": "move", "block_id": "blk-00000003", "direction": "up"}
    )
    assert _block_ids(moved.json()) == [
        "blk-00000001",
        "blk-00000003",
        "blk-00000002",
        "blk-00000004",
    ]
    clamped = await client.patch(
        url, json={"op": "move", "block_id": "blk-00000001", "direction": "up"}
    )
    assert _block_ids(clamped.json()) == [
        "blk-00000001",
        "blk-00000003",
        "blk-00000002",
        "blk-00000004",
    ]

    no_direction = await client.patch(url, json={"op": "move", "block_id": "blk-00000001"})
    assert no_direction.status_code == 422
    assert no_direction.json()["error"]["code"] == "invalid_request"
    missing = await client.patch(
        url, json={"op": "move", "block_id": "blk-00000009", "direction": "up"}
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    unknown_op = await client.patch(url, json={"op": "rotate", "block_id": "blk-00000001"})
    assert unknown_op.status_code == 422
    assert unknown_op.json()["error"]["code"] == "invalid_request"


async def test_update_block_rewrites_payload_and_clears_attempts(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = _blocks_url(book_id, page_id)

    # 先把 quiz 答一次（改稿后这条作答要跟着清掉）
    await client.post(
        f"/api/v1/books/{book_id}/pages/{page_id}/attempts",
        json={"block_id": "blk-00000002", "answer": ["A"]},
    )
    quiz2 = {
        "stem": "频谱图的横轴表示什么？",
        "options": [{"key": "A", "text": "频率"}, {"key": "B", "text": "时间"}],
        "answer_key": ["A"],
        "explanation": "横轴是频率。",
    }
    updated = await client.patch(
        url, json={"op": "update", "block_id": "blk-00000002", "payload": quiz2}
    )
    assert updated.status_code == 200
    block = updated.json()["blocks"][1]
    assert block["status"] == "done" and block["payload"]["stem"] == "频谱图的横轴表示什么？"
    left = await app.state.db.fetch_one(
        "SELECT COUNT(*) AS n FROM book_attempts WHERE block_id = ?", ("blk-00000002",)
    )
    assert int(left["n"]) == 0

    # 用户改稿也要过 figure 家族的深校验：没有 mermaid 围栏直接 422
    bad_figure = await client.patch(
        url,
        json={"op": "update", "block_id": "blk-00000003", "payload": {"markdown": "只有文字"}},
    )
    assert bad_figure.status_code == 422
    assert bad_figure.json()["error"]["code"] == "invalid_payload"
    assert "围栏" in bad_figure.json()["error"]["message"]

    no_payload = await client.patch(url, json={"op": "update", "block_id": "blk-00000001"})
    assert no_payload.status_code == 422
    assert no_payload.json()["error"]["code"] == "invalid_request"


async def test_delete_and_insert_blocks(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = _blocks_url(book_id, page_id)

    deleted = await client.patch(url, json={"op": "delete", "block_id": "blk-00000003"})
    assert _block_ids(deleted.json()) == ["blk-00000001", "blk-00000002", "blk-00000004"]

    # 插一块 note 到第一块后面：手写块建好即成稿
    inserted = await client.patch(
        url,
        json={
            "op": "insert",
            "type": "note",
            "after_block_id": "blk-00000001",
            "payload": {"markdown": "自己的备注"},
        },
    )
    blocks = inserted.json()["blocks"]
    assert [block["type"] for block in blocks] == ["text", "note", "quiz", "flashcard"]
    assert blocks[1]["status"] == "done" and blocks[1]["payload"]["markdown"] == "自己的备注"

    # 插一块 text 不给 payload：待生成占位，追加到末尾
    placeholder = await client.patch(url, json={"op": "insert", "type": "text", "focus": "补一段"})
    tail = placeholder.json()["blocks"][-1]
    assert tail["type"] == "text" and tail["status"] == "pending" and tail["focus"] == "补一段"

    bad_type = await client.patch(url, json={"op": "insert", "type": "video"})
    assert bad_type.status_code == 422
    assert bad_type.json()["error"]["code"] == "invalid_block_type"


async def test_regenerate_and_retype_use_llm_but_keep_block_id(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = _blocks_url(book_id, page_id)
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[TEXT2], usage=USAGE),
            ScriptedStep(chunks=[FLASH], usage=USAGE),
        ]
    )
    install_scripted(lambda: scripted)

    regen = await client.patch(url, json={"op": "regenerate", "block_id": "blk-00000001"})
    assert regen.status_code == 200
    block = regen.json()["blocks"][0]
    assert block["id"] == "blk-00000001" and block["status"] == "done"
    assert block["payload"]["markdown"] == TEXT2  # 同一块内容换新

    retyped = await client.patch(
        url, json={"op": "retype", "block_id": "blk-00000001", "type": "flashcard"}
    )
    assert retyped.status_code == 200
    block = retyped.json()["blocks"][0]
    assert block["type"] == "flashcard" and block["id"] == "blk-00000001"
    assert block["payload"]["cards"][0]["front"] == "傅里叶变换做什么？"
    assert scripted.exhausted and len(scripted.calls) == 2


async def test_retype_to_note_is_zero_llm(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = _blocks_url(book_id, page_id)
    await client.post(
        f"/api/v1/books/{book_id}/pages/{page_id}/attempts",
        json={"block_id": "blk-00000002", "answer": ["A"]},
    )
    scripted = ScriptedLLM([])
    install_scripted(lambda: scripted)

    retyped = await client.patch(
        url, json={"op": "retype", "block_id": "blk-00000002", "type": "note"}
    )
    block = retyped.json()["blocks"][1]
    assert block["type"] == "note" and block["status"] == "done"  # 白稿待用户写
    assert block["payload"] == {"markdown": ""}
    assert scripted.calls == []

    left = await app.state.db.fetch_one(
        "SELECT COUNT(*) AS n FROM book_attempts WHERE block_id = ?", ("blk-00000002",)
    )
    assert int(left["n"]) == 0  # 换成手写块，旧作答跟着清

    regen_note = await client.patch(url, json={"op": "regenerate", "block_id": "blk-00000002"})
    assert regen_note.status_code == 422
    assert regen_note.json()["error"]["code"] == "invalid_request"


async def test_regenerate_failure_marks_block_error_but_returns_200(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    scripted = ScriptedLLM([ScriptedStep(chunks=["不是 JSON"]), ScriptedStep(chunks=["还是散文"])])
    install_scripted(lambda: scripted)

    response = await client.patch(
        _blocks_url(book_id, page_id), json={"op": "regenerate", "block_id": "blk-00000002"}
    )
    assert response.status_code == 200  # 内容问题不改 HTTP：错误记在块上给界面看
    block = response.json()["blocks"][1]
    assert block["status"] == "error" and "两次都没生成成功" in block["error"]
    assert scripted.exhausted


async def test_block_edit_is_locked_while_compiling(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app, status="compiling")
    busy = await client.patch(
        _blocks_url(book_id, page_id), json={"op": "delete", "block_id": "blk-00000003"}
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "compile_busy"
    assert busy.json()["error"]["recoverable"] is True


async def test_attempts_scoring_progress_and_errors(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    posts = f"/api/v1/books/{book_id}/pages/{page_id}/attempts"

    wrong = await client.post(posts, json={"block_id": "blk-00000002", "answer": ["B"]})
    assert wrong.status_code == 200
    assert wrong.json()["correct"] is False
    assert wrong.json()["explanation"] == "横轴是频率，纵轴是振幅。"
    await client.patch(f"/api/v1/books/{book_id}/pages/{page_id}", json={"visited": True})

    progress = (await client.get(f"/api/v1/books/{book_id}")).json()["progress"]
    assert progress["completed"] == 1 and progress["completion"] == 1.0
    assert progress["quizzes"] == {"total": 1, "answered": 1, "correct": 0}
    assert progress["weak_chapters"][0]["chapter_key"] == "ch-1"  # 答错了：进薄弱章

    # 重做对了：每块只认最近一次，薄弱章消失
    right = await client.post(posts, json={"block_id": "blk-00000002", "answer": ["a"]})
    assert right.json()["correct"] is True  # 小写选项 key 归一
    progress = (await client.get(f"/api/v1/books/{book_id}")).json()["progress"]
    assert progress["quizzes"]["correct"] == 1 and progress["weak_chapters"] == []

    # 页详情带每块最近一次作答：阅读器刷新/翻页回来还能显示答没答对
    page = (await client.get(f"/api/v1/books/{book_id}/pages/{page_id}")).json()
    assert page["attempts"] == [{"block_id": "blk-00000002", "answer": ["A"], "correct": True}]

    missing = await client.post(posts, json={"block_id": "blk-00000009", "answer": ["A"]})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    not_quiz = await client.post(posts, json={"block_id": "blk-00000001", "answer": ["A"]})
    assert not_quiz.status_code == 422
    assert not_quiz.json()["error"]["code"] == "invalid_block"
    empty = await client.post(posts, json={"block_id": "blk-00000002", "answer": []})
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "invalid_request"


async def test_chat_endpoint_stores_history_and_usage(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = f"/api/v1/books/{book_id}/pages/{page_id}/chat"
    page_url = f"/api/v1/books/{book_id}/pages/{page_id}"
    scripted = ScriptedLLM([ScriptedStep(chunks=["横轴是频率。"], usage=USAGE)])
    install_scripted(lambda: scripted)

    answer = await client.post(url, json={"message": "横轴是什么？"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["answer"] == "横轴是频率。"
    assert body["message_id"].startswith("bmsg-") and body["degraded"] is None

    page = (await client.get(page_url)).json()
    assert [item["role"] for item in page["messages"]] == ["user", "assistant"]
    assert page["messages"][0]["content_md"] == "横轴是什么？"
    # 记账：合成 turn_id（不建会话、不写消息，照 co_writer 先例）
    row = await app.state.db.fetch_one(
        "SELECT * FROM usage_records WHERE turn_id = ?", (f"bookchat-{body['message_id']}",)
    )
    assert row is not None and int(row["input_tokens"]) == 120

    # 第二问带上第一轮历史（一问一答在系统提示词之后）
    scripted2 = ScriptedLLM([ScriptedStep(chunks=["还是频率。", "纵轴是振幅。"], usage=USAGE)])
    install_scripted(lambda: scripted2)
    second = await client.post(url, json={"message": "再说一遍？"})
    assert second.json()["answer"] == "还是频率。纵轴是振幅。"
    messages = scripted2.calls[0].messages
    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "横轴是什么？" and messages[2]["content"] == "横轴是频率。"

    empty = await client.post(url, json={"message": "   "})
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "invalid_request"
    gone = await client.post(
        "/api/v1/books/bk-00000000/pages/bp-00000001/chat", json={"message": "在吗"}
    )
    assert gone.status_code == 404


async def test_chat_busy_per_page(app_client):
    app, client = app_client
    book_id, page_id = await _seed(app)
    url = f"/api/v1/books/{book_id}/pages/{page_id}/chat"
    scripted = ScriptedLLM([ScriptedStep(chunks=["慢答。"], usage=USAGE, delay_ms=500)])
    install_scripted(lambda: scripted)

    first = asyncio.create_task(client.post(url, json={"message": "第一问"}))
    for _ in range(200):
        if scripted.calls:
            break
        await asyncio.sleep(0.01)
    assert scripted.calls, "页聊天没按预期开跑"
    busy = await client.post(url, json={"message": "第二问"})
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "chat_busy"
    assert busy.json()["error"]["recoverable"] is True
    assert (await first).json()["answer"] == "慢答。"
    # 跑完就放锁：再来一问正常
    scripted2 = ScriptedLLM([ScriptedStep(chunks=["答完了。"], usage=USAGE)])
    install_scripted(lambda: scripted2)
    assert (await client.post(url, json={"message": "第三问"})).status_code == 200


async def test_export_markdown_structure(app_client, monkeypatch):
    app, client = app_client
    book_id, page_id = await _seed(app)

    response = await client.get(f"/api/v1/books/{book_id}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert f'filename="{book_id}.md"' in response.headers["content-disposition"]
    text = response.text
    assert text.startswith("# 傅里叶入门")
    assert "## 频域的世界" in text and "> 频谱怎么读" in text
    assert "傅里叶变换把信号拆成频率来看。" in text
    # quiz 题目在章内、答案与解析汇到章末参考答案
    assert "**频谱图的横轴是什么？**" in text and "- A. 频率" in text
    assert "### 参考答案" in text and "1. **A** — 横轴是频率，纵轴是振幅。" in text
    assert "```mermaid" in text  # figure 围栏原样保留（回 nnnu 照旧渲染）

    # 删掉 figure 再导：没了的块自然不进导出
    await client.patch(
        _blocks_url(book_id, page_id), json={"op": "delete", "block_id": "blk-00000003"}
    )
    assert "```mermaid" not in (await client.get(f"/api/v1/books/{book_id}/export")).text

    # 语言随界面设置：切成英文标签（正文还是原来的中文内容）
    class FakeSettingsService:
        def load_area(self, area: str) -> dict:
            return {"ui_language": "en"}

    from nnnu.api.routers import book as book_router

    monkeypatch.setattr(book_router, "get_settings_service", lambda: FakeSettingsService())
    english = (await client.get(f"/api/v1/books/{book_id}/export")).text
    assert "### Answers" in english and "### 参考答案" not in english

    assert (await client.get("/api/v1/books/bk-00000000/export")).status_code == 404


async def test_health_is_silent_without_snapshot_and_reports_drift(app_client):
    app, client = app_client
    book_id, _ = await _seed(app)
    # 直插的书没有指纹快照：健康检查不硬报（前端就不显示横幅）
    assert (await client.get(f"/api/v1/books/{book_id}")).json()["health"] is None

    await app.state.db.execute(
        "UPDATE books SET fingerprints = ?, sources = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "kbs": {
                        "kb-00000001": {
                            "name": "教材库",
                            "docs": {"kbdoc-1": {"fp": "sha256:old", "filename": "第1章.md"}},
                        }
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps({"kbs": ["kb-00000001"]}, ensure_ascii=False),
            book_id,
        ),
    )
    # 快照里的库早没了（测试环境没建过 KB）：整库算 missing，横幅该亮
    health = (await client.get(f"/api/v1/books/{book_id}")).json()["health"]
    assert health["status"] == "drift"
    assert health["drift"][0]["change"] == "missing"
    assert health["drift"][0]["label"] == "教材库·第1章.md"


async def _wait_block_done(app, client, book_id: str, page_id: str, block_id: str) -> dict:
    for _ in range(600):
        page = (await client.get(f"/api/v1/books/{book_id}/pages/{page_id}")).json()
        block = next(item for item in page["blocks"] if item["id"] == block_id)
        if block["status"] != "compiling":
            return block
        await asyncio.sleep(0.05)
    raise AssertionError("块生成超时没收工")


async def test_animation_regenerate_returns_202_then_lands(manim_app_client):
    app, client = manim_app_client
    book_id, page_id = await _seed(app, blocks=[ANIM_BLOCK])
    url = _blocks_url(book_id, page_id)
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[ANALYSIS], usage=USAGE, delay_ms=500),
            ScriptedStep(chunks=[DESIGN], usage=USAGE),
            ScriptedStep(chunks=[ANIM_CODE], usage=USAGE),
            ScriptedStep(chunks=[ANIM_SUMMARY], usage=USAGE),
        ]
    )
    install_scripted(lambda: scripted)

    started = await client.patch(url, json={"op": "regenerate", "block_id": "blk-00000001"})
    assert started.status_code == 202
    assert started.json() == {"started": True, "block_id": "blk-00000001"}
    # 第一段 LLM 还在路上时再点一次：这个块算忙态
    for _ in range(200):
        if scripted.calls:
            break
        await asyncio.sleep(0.01)
    busy = await client.patch(url, json={"op": "regenerate", "block_id": "blk-00000001"})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "compile_busy"

    block = await _wait_block_done(app, client, book_id, page_id, "blk-00000001")
    assert block["status"] == "done"
    body = block["payload"]["markdown"]
    assert "```nnnu-artifact" in body and ANIM_CODE in body
    artifact = extract_artifact_json(body)
    assert artifact is not None and artifact["url"].startswith("/api/v1/renders/rnd-")
    assert scripted.exhausted and len(scripted.calls) == 4
    # 动画那 4 次调用记在块任务这笔账上
    row = await app.state.db.fetch_one(
        "SELECT SUM(input_tokens) AS n FROM usage_records WHERE turn_id = ?",
        ("book-block-blk-00000001",),
    )
    assert int(row["n"]) == 4 * 120
