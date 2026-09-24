"""question_bank 工具（§7.2）：搜题与写题两个动作。

工具是「聊天 → 题库」的出口：模型在聊天里说「你上次错的那道题」时要真查得到，
说「把这道题存起来」时要真写得进。这里盯四件事：
搜索的筛选与渲染、写题的来源标记（tool + 会话 id）、入库失败的原样回报、未知动作。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext, ToolMount
from nnnu.tools.builtin.question_bank_tool import QuestionBankTool

SINGLE = {
    "stem": "函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？",
    "type": "single",
    "options": ["2", "4", "8", "16"],
    "answer": "B",
    "explanation": "先求导得 $2x$，代入 $x=2$ 得 4。",
    "knowledge_point": "导数",
}


@pytest.fixture
async def bank(tmp_home):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


def _ctx(**args) -> ToolContext:
    return ToolContext(turn_id="turn-1", session_id="sess-bank", language="zh", args=args)


async def test_search_renders_hits_with_marks(bank):
    _client, app = bank
    question = await app.state.questions.create_question(**SINGLE)
    result = await QuestionBankTool().run(_ctx(action="search"))

    assert result.ok is True
    assert question.stem[:20] in result.output
    assert f"id={question.id}" in result.output
    assert "未作答" in result.output  # 还没作答过的题带这个标记
    assert result.detail is not None
    assert result.detail["questions"][0]["knowledge_point"] == "导数"


async def test_search_filter_wrong_and_empty_result(bank):
    _client, app = bank
    service = app.state.questions
    wrong = await service.create_question(**SINGLE)
    await service.create_question(
        **{**SINGLE, "stem": r"另一道关于导数的题：$\sin x$ 的导数？", "answer": "A"}
    )
    await service.record_attempt(
        wrong.id, answer="D", correct=False, score=0.0, source="deterministic"
    )

    only_wrong = await QuestionBankTool().run(_ctx(action="search", filter="wrong"))
    assert only_wrong.output.count("id=") == 1
    assert f"id={wrong.id}" in only_wrong.output
    assert "错过 1 次" in only_wrong.output

    by_point = await QuestionBankTool().run(_ctx(action="search", knowledge_point="导数"))
    assert by_point.output.count("id=") == 2

    none = await QuestionBankTool().run(_ctx(action="search", query="不存在的关键词"))
    assert none.ok is True
    assert "没有符合条件" in none.output
    assert none.detail is None


async def test_add_stores_with_tool_source(bank):
    _client, app = bank
    tool = QuestionBankTool()
    result = await tool.run(
        _ctx(
            action="add",
            questions=[
                {**SINGLE, "stem": "新加的一道题：$\\cos x$ 的导数是多少？"},
                {"stem": "简答题：说明导数的几何意义。", "type": "short", "answer": "切线斜率"},
            ],
        )
    )
    assert result.ok is True, result.output
    assert "已入库 2 道题" in result.output

    rows = await app.state.db.fetch_all("SELECT id, stem, source, session_id FROM questions")
    assert len(rows) == 2
    assert {row["source"] for row in rows} == {"tool"}
    assert {row["session_id"] for row in rows} == {"sess-bank"}
    for row in rows:
        assert row["id"] in result.output


async def test_add_reports_validation_errors(bank):
    _client, _app = bank
    tool = QuestionBankTool()
    bad = await tool.run(
        _ctx(action="add", questions=[{**SINGLE, "answer": "E"}])  # 标签越界
    )
    assert bad.ok is False
    assert "入库失败" in bad.output
    assert (await tool.run(_ctx(action="add"))).ok is False
    assert (await tool.run(_ctx(action="add", questions=["不是对象"]))).ok is False


async def test_unknown_action_and_mount(bank):
    _client, _app = bank
    result = await QuestionBankTool().run(_ctx(action="delete"))
    assert result.ok is False
    assert "未知动作" in result.output
    assert QuestionBankTool.definition.mount is ToolMount.USER_TOGGLEABLE


async def test_search_respects_limit(bank):
    _client, app = bank
    for index in range(3):
        await app.state.questions.create_question(
            **{**SINGLE, "stem": f"第 {index} 道关于导数的题：求 $x^{index}$ 的导数？"}
        )
    result = await QuestionBankTool().run(_ctx(action="search", limit=2))
    assert result.output.count("id=") == 2
    assert len(result.detail["questions"]) == 2
