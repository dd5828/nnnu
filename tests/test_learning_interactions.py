"""答题交互台账（§7.5）：**一条路径同一时刻只有一张卡在飞**的唯一真源。

`learning_interactions` 干两件事：记判分来源（判过没判过、谁判的、判成什么），
以及用一条**部分唯一索引**把「一卡在飞」变成数据库事实——而不是靠工具自觉。
这里盯四件事（出卡/判分流程本身在 test_mastery_tool，金路径在 test_learning_pipeline）：
- 部分索引真的拦得住第二条未决行，且只拦未决行（判过/作废的行不限条数）；
- 开新卡自动作废旧未决（旧行留着当审计，不是删掉）；
- 删节点带走未决状态，但**不留白**：审计行活过节点删除；
- 回合被停/超时之后那张卡还在，下一回合被优先重提，且重出的是同一道题。
"""

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.learning.quiz import pick_next_question

DAY = 86400.0
NOW = 1_700_000_000.0  # 用例显式传时间，别跟墙上时间赛跑

LEAF = [{"title": "矩阵与初等变换", "type": "procedure"}]
TWO = [
    {"title": "向量与线性组合", "type": "concept"},
    {"title": "矩阵与初等变换", "type": "procedure", "parent_index": 0},
]


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


def _insert_sql(status: str) -> str:
    return (
        "INSERT INTO learning_interactions (id, path_id, node_id, question_id, kind, status, "
        "card_prompt, user_answer, correct, score, feedback, grade_source, session_id, turn_id, "
        f"created_at, answered_at, updated_at) VALUES (?, ?, ?, '', 'quiz', '{status}', "
        "'', '', NULL, NULL, '', '', NULL, NULL, 1.0, NULL, 1.0)"
    )


async def _raw_insert(db, interaction_id: str, *, path_id: str, node_id: str, status: str) -> None:
    """绕过服务层直接写一行——要验的正是**数据库**那层的约束。"""
    await db.execute(_insert_sql(status), (interaction_id, path_id, node_id))


async def test_partial_unique_index_allows_one_pending_per_path(hub):
    _client, app = hub
    learning = app.state.learning
    path, nodes = await learning.create_path(topic="线代", nodes=TWO, now=NOW)
    other, _ = await learning.create_path(topic="概率论", nodes=LEAF, now=NOW)

    await _raw_insert(
        app.state.db, "lint-a", path_id=path.id, node_id=nodes[0].id, status="awaiting_input"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await _raw_insert(
            app.state.db, "lint-b", path_id=path.id, node_id=nodes[1].id, status="awaiting_input"
        )
    # 另一条路径互不影响；同路径的「已判/作废」行也不受这条部分索引管（审计要能堆）
    await _raw_insert(
        app.state.db, "lint-other", path_id=other.id, node_id=nodes[0].id, status="awaiting_input"
    )
    await _raw_insert(
        app.state.db, "lint-done", path_id=path.id, node_id=nodes[0].id, status="graded"
    )
    await _raw_insert(
        app.state.db, "lint-gone", path_id=path.id, node_id=nodes[0].id, status="abandoned"
    )
    pending = await learning.pending_interaction(path.id)
    assert pending is not None and pending.id == "lint-a"


async def test_open_interaction_replaces_previous_pending(hub):
    _client, app = hub
    learning = app.state.learning
    path, nodes = await learning.create_path(topic="线代", nodes=LEAF, now=NOW)

    first = await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id="q-1",
        kind="quiz",
        card_prompt="第一题",
        now=NOW,
    )
    second = await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id="q-2",
        kind="quiz",
        card_prompt="第二题",
        now=NOW + 1,
    )

    stale = await learning.get_interaction(first.id)
    assert stale.status == "abandoned"  # 旧行留着当审计（不是删掉）
    assert stale.user_answer == "" and stale.correct is None  # 但没被判过：不挂账
    pending = await learning.pending_interaction(path.id)
    assert pending.id == second.id and pending.card_prompt == "第二题"
    rows = await app.state.db.fetch_all(
        "SELECT id FROM learning_interactions WHERE path_id = ? AND status = 'awaiting_input'",
        (path.id,),
    )
    assert [row["id"] for row in rows] == [second.id]


async def test_delete_node_abandons_pending_but_keeps_ledger(hub):
    """软引用：节点没了，未决卡作废，但交互行本身留下（审计不随节点陪葬）。"""
    _client, app = hub
    learning = app.state.learning
    path, nodes = await learning.create_path(topic="线代", nodes=LEAF, now=NOW)
    interaction = await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id="q-1",
        kind="quiz",
        card_prompt="题干",
        now=NOW,
    )

    assert await learning.delete_node(nodes[0].id) is True
    kept = await learning.get_interaction(interaction.id)
    assert kept is not None and kept.status == "abandoned"
    assert kept.node_id == nodes[0].id  # 指向已删节点：软引用不置空（它记的是历史）
    assert await learning.pending_interaction(path.id) is None
    # 双保险：已作废的行不会再把下一目标劫持成 answer_pending
    assert (await learning.next_objective(path.id, now=NOW)).action != "answer_pending"


async def test_close_interaction_is_idempotent(hub):
    _client, app = hub
    learning = app.state.learning
    path, nodes = await learning.create_path(topic="线代", nodes=LEAF, now=NOW)
    interaction = await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id="q-1",
        kind="quiz",
        card_prompt="题干",
        now=NOW,
    )

    closed = await learning.close_interaction(
        interaction.id, user_answer="A", correct=True, score=1.0, feedback="答对了", now=NOW + 1
    )
    assert closed is not None and closed.status == "graded" and closed.answered_at == NOW + 1
    # 补写一次（重放）：已落定的行一个字都不改
    again = await learning.close_interaction(
        interaction.id, user_answer="B", correct=False, score=0.0, feedback="改写", now=NOW + 2
    )
    assert (again.user_answer, again.correct, again.feedback) == ("A", True, "答对了")
    assert (await learning.abandon_pending(path.id, now=NOW)) == 0  # 没有未决行可作废
    assert await learning.get_interaction("lint-ghost") is None


async def test_stopped_turn_keeps_card_for_next_turn(hub):
    """回合被停/超时：那张卡还在，下一回合**优先重提**，且重出的是同一道题。"""
    _client, app = hub
    learning, bank = app.state.learning, app.state.questions
    path, nodes = await learning.create_path(topic="线代", nodes=LEAF, now=NOW)
    question = await bank.create_question(
        stem="停回合的那道题", options=["选项一", "选项二"], answer="A", node_id=nodes[0].id
    )
    first = await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id=question.id,
        kind="quiz",
        card_prompt=question.stem,
        now=NOW,
    )

    # 回合被停：谁也没收尾这张卡。下一回合的下一目标就是它（未决优先，不是 probe/practice）
    target = await learning.next_objective(path.id, now=NOW + 1)
    assert (target.action, target.node_id) == ("answer_pending", nodes[0].id)

    # 重出题：没作答的题优先 → 还是同一道；开新卡顺手把旧未决行作废
    picked = await pick_next_question(nodes[0].id)
    assert picked is not None and picked.id == question.id
    second = await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id=picked.id,
        kind="quiz",
        card_prompt=picked.stem,
        now=NOW + 2,
    )
    assert (await learning.get_interaction(first.id)).status == "abandoned"
    assert (await learning.pending_interaction(path.id)).id == second.id

    # 答完这张：未决清空，下一目标不再被劫持
    await learning.close_interaction(
        second.id, user_answer="A", correct=True, score=1.0, feedback="答对了", now=NOW + 3
    )
    assert await learning.pending_interaction(path.id) is None
    later = await learning.next_objective(path.id, now=NOW + 4)
    assert later.action != "answer_pending"
