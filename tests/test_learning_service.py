"""学习服务（§7.5）：树、掌握度公式、四态、复习升降档、下一目标、交互台账——纯服务层。

「门就是游标」之后这一层少了两个东西：`learning_paths.current_node_id` 与 `advance`
（模型没有推进权），所以别处再也读不到「当前节点」这种列——要找下一步一律走
`next_objective`。用带 app 句柄的 client 起 lifespan（服务装配与真实运行一致），
断言直接读库/调服务；只有作答回写那条用例走 REST，验证判分端点确实会联动节点。
"""

import time

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.learning.mastery import (
    compute_state,
    display_mastery,
    node_mastery,
    trailing_streak,
)
from nnnu.services.learning.models import LearningNode
from nnnu.services.learning.policy import LADDERS, gate_of
from nnnu.services.learning.scheduler import schedule_next
from nnnu.services.learning.service import LearningError

DAY = 86400.0
# 看板聚合（get_path/list_paths）内部读的是墙上时间，用例显式传的 now 必须与之一致，
# 否则「已到期 / 未到期」会随固定时间戳落在过去而全部翻转。断言全是相对量，不依赖具体值。
NOW = time.time()

# 四类型各来一个：定量（记忆/流程）与定性（概念/设计）两条路都要有样本
SPEC = [
    {"title": "向量与线性组合", "type": "concept", "description": "线性代数的入口"},
    {"title": "矩阵与初等变换", "type": "procedure", "parent": 0},
    {"title": "行列式", "type": "memory", "parent": 0},
    {"title": "特征值分解", "type": "design", "parent": "矩阵与初等变换"},
]


@pytest.fixture
async def hub(tmp_home):
    """(client, service, bank)：服务层用例多半要同时摸两边的服务。"""
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app.state.learning, app.state.questions


async def _path(hub, nodes=None, topic="线性代数基础"):
    _, service, _ = hub
    return await service.create_path(topic=topic, nodes=nodes or SPEC, now=NOW)


async def _answer(service, bank, node_id, *, score, count=1, question_type="single"):
    """给节点挂 count 道题、每道按 score 作答一次，然后回写一次掌握度并返回节点。

    题数决定证据封顶，所以「几题」必须由用例显式说了算（3 题才可能拿到满分）。
    `on_attempt` 不再收 score：分数从刚落进 `question_attempts` 的那一行读，幂等可重放。
    """
    for index in range(count):
        options = [] if question_type == "short" else ["1", "2"]
        answer = "参考答案" if question_type == "short" else "A"
        question = await bank.create_question(
            stem=f"第 {index + 1} 题：{node_id} 的练习",
            options=options,
            answer=answer,
            type=question_type,
            node_id=node_id,
        )
        await bank.record_attempt(
            question.id,
            answer=answer,
            correct=score >= 0.6,
            score=score,
            source="deterministic",
        )
    return await service.on_attempt(node_id, now=NOW)


# ---- 建树 ----


async def test_create_path_builds_tree_in_preorder(hub):
    path, nodes = await _path(hub)
    assert path.id.startswith("lpath-")
    # 深度优先前序：子节点紧跟父节点（规格里的数组序若不合前序，落库时归一）
    assert [node.title for node in nodes] == [
        "向量与线性组合",
        "矩阵与初等变换",
        "特征值分解",
        "行列式",
    ]
    assert [node.depth for node in nodes] == [0, 1, 2, 1]
    assert [node.sort_order for node in nodes] == [0, 1, 2, 3]  # 全路径前序连续整数
    assert nodes[1].parent_id == nodes[0].id
    assert nodes[2].parent_id == nodes[1].id  # 父节点用标题指也能挂上
    assert nodes[3].parent_id == nodes[0].id  # 下标指的父节点同理
    assert nodes[3].id.startswith("lnode-")
    # 路径本体上没有任何「当前节点」——门就是游标，下一步现算
    assert not hasattr(path, "current_node_id")


async def test_create_path_rejects_bad_specs(hub):
    _, service, _ = hub
    with pytest.raises(LearningError, match="主题不能为空"):
        await service.create_path(topic="  ", nodes=SPEC)
    with pytest.raises(LearningError, match="至少要有一个节点"):
        await service.create_path(topic="空路径", nodes=[])
    with pytest.raises(LearningError, match="未知节点类型"):
        await service.create_path(topic="坏类型", nodes=[{"title": "A", "type": "essay"}])
    with pytest.raises(LearningError, match="父下标"):
        await service.create_path(topic="坏父", nodes=[{"title": "A"}, {"title": "B", "parent": 5}])
    with pytest.raises(LearningError, match="不在它前面"):
        await service.create_path(topic="坏父", nodes=[{"title": "A", "parent": "还没出现"}])


async def test_add_move_and_delete_node(hub):
    path, nodes = await _path(hub)
    _, service, _ = hub
    # 挂第一个节点下：成为它最后一个孩子（前序 = 父节点整棵子树之后），编号连续
    added = await service.add_node(path.id, title="向量空间", parent_id=nodes[0].id, now=NOW)
    assert (added.depth, added.sort_order) == (1, 4)
    titles = [node.title for node in await service.list_nodes(path.id)]
    assert titles == ["向量与线性组合", "矩阵与初等变换", "特征值分解", "行列式", "向量空间"]

    # 上移一位：与同父的前一个兄弟「行列式」换位置（特征值是矩阵的孩子，跟着矩阵一起挪）
    assert await service.move_node(added.id, "up") is True
    nodes_now = await service.list_nodes(path.id)
    assert [node.title for node in nodes_now] == [
        "向量与线性组合",
        "矩阵与初等变换",
        "特征值分解",
        "向量空间",
        "行列式",
    ]
    assert [node.sort_order for node in nodes_now] == [0, 1, 2, 3, 4]
    assert [node.depth for node in nodes_now] == [0, 1, 2, 1, 1]
    # 已经在头/尾：什么也不做，也不算失败
    assert await service.move_node(added.id, "down") is True
    with pytest.raises(LearningError, match="未知方向"):
        await service.move_node(added.id, "sideways")

    # 改类型 = 换一套门（概念是定性门、流程是定量门 90 分）
    updated = await service.update_node(nodes[1].id, node_type="concept")
    assert updated is not None and updated.node_type == "concept"
    assert updated.gate is None and updated.gate_kind == "qualitative"
    again = await service.update_node(nodes[1].id, node_type="memory")
    assert again is not None and again.gate == 90.0


async def test_delete_node_cascades_and_detaches_questions(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    question = await bank.create_question(
        stem="挂在特征值分解上的题", options=["1", "2"], answer="A", node_id=nodes[2].id
    )
    # 删父节点：子树（含特征值分解）一起走，挂着的题 node_id 置空
    assert await service.delete_node(nodes[1].id) is True
    remaining = await service.list_nodes(path.id)
    assert [node.title for node in remaining] == ["向量与线性组合", "行列式"]
    assert [node.sort_order for node in remaining] == [0, 1]
    assert (await bank.get_question(question.id)).node_id is None


# ---- 掌握度与四态 ----


def test_node_mastery_recency_and_confidence_cap():
    assert node_mastery([]) == 0.0
    # 置信封顶：只答一题最多 50，两题 80，三题起才可能满分（蒙对一题不算掌握）
    assert node_mastery([1.0]) == 50.0
    assert node_mastery([1.0, 1.0]) == 80.0
    assert node_mastery([1.0, 1.0, 1.0]) == 100.0
    assert node_mastery([0.5, 0.5, 0.5, 0.5]) == 50.0
    # 近因加权：尾部的错更疼（[0,1] 比 [1,0] 高），但都受两次封顶 80
    assert node_mastery([0.0, 1.0]) == 51.3
    assert node_mastery([0.0, 1.0, 1.0]) == 69.6
    assert node_mastery([1.0, 0.0]) == 48.7


def test_node_mastery_window_slides_old_mistakes_out():
    """答对新的把旧的错挤出窗口：错一次的账号连对 5 次后回到满分。"""
    scores = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    assert node_mastery(scores) == 100.0
    # 窗口只吃尾部 5 次，所以那次 0 分连影响都留不下
    assert node_mastery(scores[:5]) < 100.0


def test_trailing_streak():
    assert trailing_streak([]) == 0
    assert trailing_streak([1.0, 1.0, 0.0]) == 0
    assert trailing_streak([0.0, 1.0, 1.0]) == 2
    # 多选的部分分不算「答对」，连对断在那里
    assert trailing_streak([1.0, 0.6, 1.0, 1.0]) == 2


def test_display_mastery_qualitative_switches_on_the_verdict():
    # 定量：算出来多少就是多少
    assert display_mastery(node_type="memory", assess_passed=False, evidence=37.5) == 37.5
    # 定性：过了记 100（门是布尔的），没过封顶 40——看板一眼看出「离过关还远」
    assert display_mastery(node_type="design", assess_passed=True, evidence=12.0) == 100.0
    assert display_mastery(node_type="design", assess_passed=False, evidence=88.0) == 40.0


def _node(**overrides) -> LearningNode:
    base = {
        "id": "lnode-1",
        "path_id": "lpath-1",
        "title": "节点",
        "node_type": "concept",
        "mastery": 0.0,
    }
    return LearningNode(**{**base, **overrides})


def test_compute_state_order():
    # ① 一次没作答 → 未开始（哪怕 next_review_at 已经到期）
    assert compute_state(_node(next_review_at=NOW - 1), now=NOW) == "not_started"
    # ② 定性节点：过了门且到期 → 需复习
    assert (
        compute_state(
            _node(
                assess_passed=True,
                mastery=100.0,
                last_practiced_at=NOW - DAY,
                next_review_at=NOW - 1,
            ),
            now=NOW,
        )
        == "reviewing"
    )
    # ③ 定量节点：恰等门算已掌握（memory 的门是 90）
    assert (
        compute_state(
            _node(
                node_type="memory",
                mastery=90.0,
                last_practiced_at=NOW - DAY,
                next_review_at=NOW + DAY,
            ),
            now=NOW,
        )
        == "mastered"
    )
    # ④ 差一点点 → 学习中（没过门就不是「需复习」，它归下一目标的补学分支）
    assert (
        compute_state(
            _node(
                node_type="memory",
                mastery=89.9,
                last_practiced_at=NOW - DAY,
                next_review_at=NOW - 1,
            ),
            now=NOW,
        )
        == "learning"
    )


def test_schedule_next_ladder():
    now = NOW
    memory = LADDERS["memory"]
    # 没过门：档位不动，固定按首档间隔排（没过门时谈阶梯没有意义）
    assert schedule_next(
        node_type="memory", review_stage=3, score=1.0, cleared=False, streak=5, now=now
    ) == (3, now + memory[0] * DAY)
    # 答错（< 0.5）：退一档且立刻到期——这就是「答错后下一轮给针对性讲解」的触发点
    assert schedule_next(
        node_type="memory", review_stage=2, score=0.0, cleared=True, streak=0, now=now
    ) == (1, now)
    assert schedule_next(
        node_type="memory", review_stage=0, score=0.2, cleared=True, streak=0, now=now
    ) == (0, now)
    # 过了门：连对 ≥2 次跳两档，否则进一档
    assert schedule_next(
        node_type="memory", review_stage=0, score=1.0, cleared=True, streak=2, now=now
    ) == (2, now + memory[2] * DAY)
    assert schedule_next(
        node_type="memory", review_stage=0, score=1.0, cleared=True, streak=1, now=now
    ) == (1, now + memory[1] * DAY)
    # 中间地带（多选部分分）：不算错也不算连对，只进一档
    assert schedule_next(
        node_type="memory", review_stage=1, score=0.55, cleared=True, streak=0, now=now
    ) == (2, now + memory[2] * DAY)
    # 末档封顶：跳两档也不会越界
    last = len(memory) - 1
    assert schedule_next(
        node_type="memory", review_stage=last, score=1.0, cleared=True, streak=9, now=now
    ) == (last, now + memory[last] * DAY)


# ---- 作答回写 ----


async def test_on_attempt_updates_mastery_and_review(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    node = nodes[3]  # 行列式：memory，定量门 90
    updated = await _answer(service, bank, node.id, score=1.0, count=3)
    assert updated is not None
    assert updated.mastery == 100.0
    assert updated.state == "mastered"
    assert updated.review_stage == 2  # 三题全对：尾部连对 3 ≥ 2，进两档
    assert updated.next_review_at == NOW + LADDERS["memory"][2] * DAY

    # 再答错一次：答错退档 + 立刻到期（下一次一定是复习，不是新内容）
    for question in await bank.list_questions(node_id=node.id):
        await bank.record_attempt(
            question.id, answer="B", correct=False, score=0.0, source="deterministic"
        )
    dropped = await service.on_attempt(node.id, now=NOW + DAY)
    assert dropped is not None
    assert dropped.mastery < 90.0  # 混进错题后掉出定量门
    assert dropped.review_stage == 1
    assert dropped.next_review_at == NOW + DAY
    assert dropped.state == "learning"  # 没过门 → 上课时说的「需复习」只给已过门的节点


async def test_on_attempt_is_idempotent_and_ignores_missing_node(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    await _answer(service, bank, nodes[0].id, score=1.0, count=2)
    first = await service.on_attempt(nodes[0].id, now=NOW)
    second = await service.on_attempt(nodes[0].id, now=NOW)
    assert first is not None and second is not None
    assert (first.mastery, first.review_stage) == (second.mastery, second.review_stage)
    # 挂空节点（题没挂路径）不是错误，只是没什么可回写的
    assert await service.on_attempt(None) is None
    assert await service.on_attempt("lnode-不存在") is None


async def test_attempt_endpoint_syncs_node_mastery(hub):
    """题库页作答 → 节点掌握度联动（REST 那条通路也验一遍）。"""
    client, service, bank = hub
    path, nodes = await _path(hub)
    question = await bank.create_question(
        stem="矩阵乘法要求什么？",
        options=["行数相等", "列数等于行数", "列数相等", "随便"],
        answer="B",
        node_id=nodes[1].id,
    )
    response = await client.post(
        f"/api/v1/questions/{question.id}/attempt", json={"answer": "B", "language": "zh"}
    )
    assert response.status_code == 200, response.text
    node = await service.get_node(nodes[1].id)
    assert node is not None
    assert node.mastery == 50.0  # 一道题答对：证据封顶 50
    assert node.state == "learning"  # 流程类门 90，还差得远


# ---- 定性门 ----


async def test_record_qualitative_sets_and_revokes(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    node = nodes[0]  # 概念：定性门
    passed = await service.record_qualitative(node.id, passed=True, now=NOW)
    assert passed is not None
    assert (passed.assess_passed, passed.assessed_at) == (True, NOW)
    assert passed.mastery == 100.0  # 过了就记 100（门是布尔的，没有中间分）
    assert passed.state == "mastered"
    assert passed.cleared is True

    # 答过题之后评「没过」：展示分被封在 40（否则一份 100 分的证据会装成有进度）
    await _answer(service, bank, node.id, score=1.0, count=3)
    failed = await service.record_qualitative(node.id, passed=False, now=NOW + DAY)
    assert failed is not None
    assert (failed.assess_passed, failed.cleared) == (False, False)
    assert failed.mastery == 40.0
    assert failed.state == "learning"


async def test_record_qualitative_without_evidence_scores_zero(hub):
    """一次没答过就没证据可展示：min(0, 40) = 0，不凭空给 40 分。"""
    path, nodes = await _path(hub)
    _, service, _ = hub
    failed = await service.record_qualitative(nodes[0].id, passed=False, now=NOW)
    assert failed is not None and failed.mastery == 0.0


async def test_record_qualitative_rejects_quantitative_node(hub):
    path, nodes = await _path(hub)
    _, service, _ = hub
    with pytest.raises(LearningError, match="定量节点"):
        await service.record_qualitative(nodes[3].id, passed=True, now=NOW)


# ---- 下一目标（门就是游标） ----


async def test_next_objective_probe_then_practice_then_review(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    # ① 全新路径：前序第一个没过门、且从没碰过的定量节点 → probe。概念节点在前，
    #    但它不会「摸一下就好」，所以这里落到它身上的动作是 assess
    target = await service.next_objective(path.id, now=NOW)
    assert (target.action, target.node_id) == ("assess", nodes[0].id)
    assert target.gate is None and target.gate_kind == "qualitative"

    # ② 定性节点过门 → 前序下一个（procedure，定量）从没碰过 → probe
    await service.record_qualitative(nodes[0].id, passed=True, now=NOW)
    target = await service.next_objective(path.id, now=NOW)
    assert (target.action, target.node_id) == ("probe", nodes[1].id)
    assert target.gate == 90.0 and target.gate_kind == "quantitative"

    # ③ 碰过一次就不再是摸底（答错也算碰过）→ practice
    await _answer(service, bank, nodes[1].id, score=0.0, count=1)
    target = await service.next_objective(path.id, now=NOW)
    assert (target.action, target.node_id) == ("practice", nodes[1].id)


async def test_next_objective_walks_preorder_and_completes(hub):
    path, nodes = await _path(hub, nodes=[{"title": "甲"}, {"title": "乙"}, {"title": "丙"}])
    _, service, _ = hub
    for node in nodes:
        await service.record_qualitative(node.id, passed=True, now=NOW)
    target = await service.next_objective(path.id, now=NOW)
    assert target.action == "complete" and target.node_id is None

    # 其中一个被「评定撤销」→ 回到它身上（前序第一个没过门）
    await service.record_qualitative(nodes[1].id, passed=False, now=NOW)
    target = await service.next_objective(path.id, now=NOW)
    assert (target.action, target.node_id) == ("assess", nodes[1].id)


async def test_next_objective_prefers_pending_interaction(hub):
    path, nodes = await _path(hub)
    _, service, _ = hub
    interaction = await service.open_interaction(
        path_id=path.id, node_id=nodes[1].id, question_id="q-x", kind="quiz", card_prompt="题干"
    )
    target = await service.next_objective(path.id, now=NOW)
    assert target.action == "answer_pending" and target.node_id == nodes[1].id
    # 落定之后回到正常顺序（不再被未决卡劫持）
    await service.close_interaction(
        interaction.id, user_answer="A", correct=True, score=1.0, feedback="ok"
    )
    target = await service.next_objective(path.id, now=NOW)
    assert target.action != "answer_pending"


async def test_next_objective_prefers_due_review_over_new_content(hub):
    path, nodes = await _path(hub)
    _, service, _ = hub
    # 甲：定性过门 → 排上复习（首档立刻算未来）；把它推到过期
    await service.record_qualitative(nodes[0].id, passed=True, now=NOW - 10 * DAY)
    node = await service.get_node(nodes[0].id)
    assert node.next_review_at is not None and node.next_review_at <= NOW
    target = await service.next_objective(path.id, now=NOW)
    assert (target.action, target.node_id) == ("review", nodes[0].id)
    assert target.due_at is not None


async def test_unbind_session_keeps_progress(hub):
    path, nodes = await _path(hub)
    _, service, _ = hub
    await service.bind_session(path.id, "sess-a")
    assert (await service.get_path_by_session("sess-a")).id == path.id
    await service.record_qualitative(nodes[0].id, passed=True, now=NOW)

    unbound = await service.unbind_session(path.id)
    assert unbound is not None and unbound.session_id is None
    assert await service.get_path_by_session("sess-a") is None
    # 脱离不动别的：进度、评定、节点都留着
    assert (await service.get_node(nodes[0].id)).assess_passed is True
    assert len(await service.list_nodes(path.id)) == len(nodes)


# ---- 看板聚合 ----


async def test_detail_board_aggregates(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    await service.record_qualitative(nodes[0].id, passed=True, now=NOW)  # 概念：过门
    await _answer(service, bank, nodes[1].id, score=0.0, count=1)  # 流程：答错，没过门

    detail = await service.get_path(path.id)
    stats = detail.stats
    assert (stats.total, stats.mastered, stats.learning, stats.not_started) == (4, 1, 1, 2)
    assert stats.progress == 0.25
    assert stats.weak == 1  # 答过但没过门的那个
    assert detail.weak_points[0].node_id == nodes[1].id
    assert detail.weak_points[0].wrong == 1
    assert detail.weak_points[0].gate_kind == "quantitative"
    assert detail.weak_points[0].gap == gate_of(nodes[1].node_type) - detail.weak_points[0].mastery
    assert [item.node_id for item in detail.reviews] == [nodes[0].id]  # 只有过门的才有复习安排
    # 前序往前走到「矩阵与初等变换」：它答错过（碰过），所以是 practice 不是 probe
    assert (detail.next_target.action, detail.next_target.node_id) == ("practice", nodes[1].id)

    # 列表卡片与详情读的是同一套聚合
    summaries = await service.list_paths()
    card = next(item for item in summaries if item.path.id == path.id)
    assert card.stats.total == stats.total and card.stats.mastered == stats.mastered
    assert card.next_title == detail.next_target.node_title
    assert card.next_action == detail.next_target.action


async def test_path_by_session_binding(hub):
    path, _ = await _path(hub)
    _, service, _ = hub
    assert await service.get_path_by_session("sess-x") is None
    bound = await service.bind_session(path.id, "sess-x")
    assert bound.session_id == "sess-x"
    assert (await service.get_path_by_session("sess-x")).id == path.id


async def test_delete_path_cascades_nodes(hub):
    path, nodes = await _path(hub)
    _, service, bank = hub
    question = await bank.create_question(
        stem="挂在上面的题", options=["1", "2"], answer="A", node_id=nodes[0].id
    )
    assert await service.delete_path(path.id) is True
    assert await service.get_path_model(path.id) is None
    assert await service.list_nodes(path.id) == []
    assert (await bank.get_question(question.id)).node_id is None
