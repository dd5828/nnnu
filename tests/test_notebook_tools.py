"""笔记本工具（§7.15）：write_note / list_notebook。

工具是「聊天 → 笔记本」的出口：用户说「记下来」要真写得进默认本、
「看看我的笔记」要真翻得到。盯三件事：默认本的自动建与复用（不重复建）、
指定名称/ id 的宽容语义（名字自动建、nb- id 查无则报错）、列表三态与截断。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext, ToolMount
from nnnu.tools.builtin.notebook_tools import ListNotebookTool, WriteNoteTool


@pytest.fixture
async def app_client(tmp_home):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


def _ctx(language: str = "zh", **args) -> ToolContext:
    return ToolContext(turn_id="turn-1", session_id="sess-nb", language=language, args=args)


async def test_write_note_creates_and_reuses_default_notebook(app_client):
    _client, app = app_client
    tool = WriteNoteTool()
    result = await tool.run(_ctx(title="勾股定理", content="# 勾股定理\n\n$a^2+b^2=c^2$"))
    assert result.ok is True, result.output
    assert "速记" in result.output
    assert result.detail["notebook_created"] is True
    assert result.detail["title"] == "勾股定理"

    # 第二次写默认本：不再新建，落在同一本里
    again = await tool.run(_ctx(content="随手记：明天复习导数"))
    assert again.ok is True
    assert again.detail["notebook_created"] is False
    assert again.detail["notebook_id"] == result.detail["notebook_id"]
    # 没给标题 → 取正文首行
    assert again.detail["title"] == "随手记：明天复习导数"

    notebooks = await app.state.notebooks.list_notebooks()
    assert [notebook.name for notebook in notebooks] == ["速记"]
    records = await app.state.notebooks.list_records(notebooks[0].id)
    assert len(records) == 2
    assert {record.type for record in records} == {"note"}
    assert {record.source_ref for record in records} == {"sess:sess-nb"}


async def test_write_note_default_name_follows_language(app_client):
    _client, app = app_client
    result = await WriteNoteTool().run(_ctx(language="en", content="Euler's identity"))
    assert result.ok is True
    assert result.detail["notebook_name"] == "Notes"


async def test_write_note_to_named_and_id_notebooks(app_client):
    _client, app = app_client
    tool = WriteNoteTool()
    # 名字不存在 → 自动建
    created = await tool.run(_ctx(notebook="错题整理", content="第一错题：符号看错"))
    assert created.ok is True
    assert created.detail["notebook_created"] is True
    # 再点名同一本 → 复用
    reused = await tool.run(_ctx(notebook="错题整理", content="第二错题：单位没换"))
    assert reused.detail["notebook_created"] is False
    assert reused.detail["notebook_id"] == created.detail["notebook_id"]
    # 按 id 指定也认
    by_id = await tool.run(_ctx(notebook=created.detail["notebook_id"], content="按 id 写的一条"))
    assert by_id.detail["notebook_id"] == created.detail["notebook_id"]


async def test_write_note_rejections(app_client):
    _client, app = app_client
    tool = WriteNoteTool()
    empty = await tool.run(_ctx(content="   "))
    assert empty.ok is False
    assert "写入笔记本失败" in empty.output
    missing = await tool.run(_ctx(notebook="nb-nope", content="x"))
    assert missing.ok is False
    assert "不存在" in missing.output
    # 名字超长（服务层校验）原样回报，不炸
    long_name = await tool.run(_ctx(notebook="长" * 61, content="x"))
    assert long_name.ok is False


async def test_list_notebook_three_modes(app_client):
    _client, app = app_client
    write = WriteNoteTool()
    first = await write.run(_ctx(notebook="甲本", title="甲一", content="甲本第一条"))
    await write.run(_ctx(notebook="乙本", title="乙一", content="乙本第一条"))
    tool = ListNotebookTool()

    overview = await tool.run(_ctx())  # 空态在下面单独测
    assert overview.ok is True
    assert "甲本" in overview.output and "乙本" in overview.output
    assert "1 条" in overview.output

    records = await tool.run(_ctx(notebook="甲本"))
    assert records.ok is True
    assert "甲一" in records.output
    assert first.detail["record_id"] in records.output
    assert "乙一" not in records.output

    full = await tool.run(_ctx(record_id=first.detail["record_id"]))
    assert full.ok is True
    assert "甲本第一条" in full.output
    assert "甲一" in full.output

    none = await tool.run(_ctx(notebook="不存在的本"))
    assert none.ok is False
    missing_record = await tool.run(_ctx(record_id="nbr-nope"))
    assert missing_record.ok is False
    assert "不存在" in missing_record.output


async def test_list_notebook_empty_state(app_client):
    _client, _app = app_client
    result = await ListNotebookTool().run(_ctx())
    assert result.ok is True
    assert "还没有任何笔记本" in result.output
    assert result.detail == {"notebooks": []}


async def test_list_notebook_truncates_long_record_and_limit(app_client):
    _client, app = app_client
    notebook = await app.state.notebooks.create_notebook("大本")
    big = await app.state.notebooks.add_record(
        notebook.id, record_type="note", title="超长", content_md="x" * 5000
    )
    result = await ListNotebookTool().run(_ctx(record_id=big.id))
    assert result.ok is True
    assert "已截断" in result.output
    assert len(result.output) < 5000

    for index in range(3):
        await app.state.notebooks.add_record(
            notebook.id, record_type="note", title=f"第{index}条", content_md="y"
        )
    listed = await ListNotebookTool().run(_ctx(notebook="大本", limit=2))
    assert listed.ok is True
    assert "共 4 条" in listed.output
    assert len(listed.detail["records"]) == 2


async def test_mount_flags(app_client):
    _client, _app = app_client
    assert WriteNoteTool.definition.mount is ToolMount.USER_TOGGLEABLE
    assert ListNotebookTool.definition.mount is ToolMount.USER_TOGGLEABLE
    assert WriteNoteTool.definition.name == "write_note"
    assert ListNotebookTool.definition.name == "list_notebook"
