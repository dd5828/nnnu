"""调研历史 REST（`/api/v1/research/runs`，§9.1 之外的两个只读端点）。

「我的历次调研」页的数据源，全是零 LLM 通路——本文件直接往仓储里塞行、往会话里塞消息，
只盯读出来的东西对不对：

- 列表新的在前、带会话标题、带大纲与失败清单；
- 详情能找回**报告那条助手消息**（确认消息之后的第一条 assistant，不是会话里最后一条、
  也不是确认之前那条）；
- 还没成稿（confirming / 没确认过）的调研 report 为 null，端点不炸；
- 不存在的 id 走 §9.1 统一错误信封。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.research.models import SubTopic
from nnnu.services.research.service import get_research_service
from nnnu.services.sessions.models import Message

TOPICS = ["RAG 架构演进", "向量数据库选型"]


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


def _subtopics() -> list[SubTopic]:
    return [
        SubTopic(title="检索链路", overview="先看召回"),
        SubTopic(title="工程取舍", overview="再看成本"),
    ]


async def _reported_run(hub, *, topic: str, session_title: str) -> dict:
    """造一次跑完的调研：建行 → 确认 → 报告消息落库，返回 {run, session_id, report_id}。"""
    _client, app = hub
    sessions = app.state.runtime._sessions
    service = get_research_service()

    session = await sessions.ensure_session(None, capability="deep_research", language="zh")
    await sessions.rename_session(session.id, session_title)
    # 确认之前先来一条 assistant（澄清/大纲）：选报告时不能被它带偏
    await sessions.append_message(
        Message.new(session_id=session.id, role="assistant", content="先澄清一下…")
    )
    run = await service.create_run(
        session_id=session.id,
        topic=topic,
        refined_topic=topic,
        mode="report",
        depth="quick",
        subtopics=_subtopics(),
    )
    answer = Message.new(session_id=session.id, role="user", content="确认")
    await sessions.append_message(answer)
    await service.confirm_run(run.id, subtopics=_subtopics(), answer_message_id=answer.id)
    report = Message.new(
        session_id=session.id,
        role="assistant",
        content="# 报告\n\n向量库选型看三点[1]。",
        citations=[
            {"doc_id": "https://example.com/a", "kb": "", "page": None, "snippet": "", "title": "A"}
        ],
    )
    await sessions.append_message(report)
    await service.finish_run(run.id, status="reported")
    return {"run": run, "session_id": session.id, "report_id": report.id}


async def test_list_runs_newest_first_with_session_title(hub):
    client, _app = hub
    first = await _reported_run(hub, topic=TOPICS[0], session_title="第一次调研")
    second = await _reported_run(hub, topic=TOPICS[1], session_title="第二次调研")

    resp = await client.get("/api/v1/research/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    runs = body["runs"]
    assert [item["id"] for item in runs] == [second["run"].id, first["run"].id]  # 新的在前
    head = runs[0]
    assert head["topic"] == TOPICS[1]
    assert head["session_title"] == "第二次调研"
    assert head["status"] == "reported"
    assert [item["title"] for item in head["subtopics"]] == ["检索链路", "工程取舍"]
    assert head["failed_subtopics"] == []

    # 翻页：offset 越过第一条只剩一条
    page2 = await client.get("/api/v1/research/runs", params={"offset": 1})
    assert [item["id"] for item in page2.json()["runs"]] == [first["run"].id]


async def test_detail_returns_report_after_confirm_message(hub):
    client, _app = hub
    made = await _reported_run(hub, topic=TOPICS[0], session_title="第一次调研")

    resp = await client.get(f"/api/v1/research/runs/{made['run'].id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_title"] == "第一次调研"
    report = body["report"]
    assert report["message_id"] == made["report_id"]  # 不是确认前那条澄清消息
    assert report["content_md"].startswith("# 报告")
    assert report["citations"][0]["title"] == "A"


async def test_detail_without_report_is_null_not_error(hub):
    client, app = hub
    service = get_research_service()
    session = await app.state.runtime._sessions.ensure_session(
        None, capability="deep_research", language="zh"
    )
    run = await service.create_run(
        session_id=session.id,
        topic="还没确认就跑了",
        refined_topic="",
        mode="report",
        depth="quick",
        subtopics=_subtopics(),
    )

    resp = await client.get(f"/api/v1/research/runs/{run.id}")
    assert resp.status_code == 200
    assert resp.json()["report"] is None  # confirming 没有确认消息 → 没有报告


async def test_unknown_run_is_404_envelope(hub):
    client, _app = hub
    resp = await client.get("/api/v1/research/runs/rrun_不存在")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_deleted_session_keeps_run_and_report_is_null(hub):
    """会话删了、调研行留着（软引用）：详情照常打得开，报告为 null。

    真机上就有这种行（列表里 status=reported 但没有报告）：文案归前端管，
    后端这边不能 500、也不能假装有成稿。
    """
    client, app = hub
    made = await _reported_run(hub, topic=TOPICS[0], session_title="会被删的会话")
    await app.state.runtime._sessions.delete_session(made["session_id"])

    resp = await client.get(f"/api/v1/research/runs/{made['run'].id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_title"] == ""  # JOIN 落空
    assert body["status"] == "reported"
    assert body["report"] is None

    listed = await client.get("/api/v1/research/runs")
    assert [item["id"] for item in listed.json()["runs"]] == [made["run"].id]
