"""学习路径 REST（§9.1 三端点 + 路径/节点直改 + 打开学习会话）。

**本文件全是零 LLM 通路**（拍板 #2/#3）：路径由 `mastery` 工具在聊天里建，
REST 只负责读看板、改树、把学习会话备好——`POST .../session` 不跑回合，
第一句由用户自己打。**没有 advance**（门就是游标），这里盯信封与副作用：
- 详情一次给全（树/汇总/薄弱点/复习/下一目标）；
- 编辑端点真改树（增删移改、级联删、题目 node_id 置空）；
- 旧的 advance 端点已经不存在（404，不是 409）；
- `POST .../session` 绑定会话但**一条消息都不写**（回归锁：它以前会代用户开口）；
- `GET /questions?node_id=` 只看这个节点的题；
- 404/422 走统一错误信封。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import uninstall_scripted

NODES = [
    {"title": "向量与线性组合", "node_type": "concept"},
    {"title": "矩阵与初等变换", "node_type": "procedure", "parent": 0},
    {"title": "特征值分解", "node_type": "design", "parent": 1},
]


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    # 本文件的端点自己不打模型，但 `repo_prompts` 留着：哪天有回合型用例落进这个文件，
    # 非可编辑安装（CI）下兜底路径探不到仓库 prompts/，断言会跟着装法静默空串（踩过一次）
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


async def _path(hub, nodes=None, topic="线性代数基础") -> dict:
    _client, app = hub
    path, _ = await app.state.learning.create_path(topic=topic, nodes=nodes or NODES)
    return path.model_dump()


async def _answer(app, node_id: str, *, score: float, count: int = 3) -> None:
    """挂 count 道题并各按 score 作答一次，最后回写一次掌握度（分数从库里读，不传）。"""
    bank = app.state.questions
    for index in range(count):
        question = await bank.create_question(
            stem=f"{node_id} 的第 {index + 1} 题",
            options=["1", "2"],
            answer="A",
            node_id=node_id,
        )
        await bank.record_attempt(
            question.id, answer="A", correct=score >= 0.6, score=score, source="deterministic"
        )
    await app.state.learning.on_attempt(node_id)


async def test_list_and_detail_include_board(hub):
    client, app = hub
    path = await _path(hub)
    listing = await client.get("/api/v1/learning/paths")
    assert listing.status_code == 200
    cards = listing.json()["paths"]
    assert len(cards) == 1
    card = cards[0]
    assert card["id"] == path["id"]
    assert card["stats"]["total"] == 3
    # 列表卡片给的是「下一目标」（不是「当前节点」——那个列已经删了）
    assert card["next_title"] == "向量与线性组合"
    assert card["next_action"] == "assess"  # 第一个节点是概念（定性门）
    assert "current_node_id" not in card

    detail = await client.get(f"/api/v1/learning/paths/{path['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert set(body) == {"path", "nodes", "stats", "weak_points", "reviews", "next_target"}
    assert [node["title"] for node in body["nodes"]] == [
        "向量与线性组合",
        "矩阵与初等变换",
        "特征值分解",
    ]
    assert [node["depth"] for node in body["nodes"]] == [0, 1, 2]
    assert body["stats"]["not_started"] == 3
    # 门与是否过门是服务端算好的计算字段，前端不镜像
    gates = [node["gate"] for node in body["nodes"]]
    assert gates == [None, 90.0, None]
    assert [node["gate_kind"] for node in body["nodes"]] == [
        "qualitative",
        "quantitative",
        "qualitative",
    ]
    assert all(node["cleared"] is False and node["due"] is False for node in body["nodes"])
    assert body["next_target"]["action"] == "assess"
    assert body["next_target"]["node_id"] == body["nodes"][0]["id"]


async def test_detail_reflects_attempts_and_weak_points(hub):
    client, app = hub
    path = await _path(hub)
    nodes = await app.state.learning.list_nodes(path["id"])
    await _answer(app, nodes[1].id, score=1.0)  # 流程：三题全对 → 过 90 的门
    await _answer(app, nodes[1].id, score=0.0, count=0)  # 不改分数，只是再回写一次
    await _answer(app, nodes[2].id, score=0.0, count=1)  # 设计：答错一道

    body = (await client.get(f"/api/v1/learning/paths/{path['id']}")).json()
    # 流程节点过门；设计节点答错一道、没过门（定性门要讲一遍）→ 它也是薄弱点
    assert (body["stats"]["mastered"], body["stats"]["weak"]) == (1, 1)
    assert [item["node_id"] for item in body["weak_points"]] == [nodes[2].id]
    assert body["weak_points"][0]["gate_kind"] == "qualitative"
    assert [item["node_id"] for item in body["reviews"]] == [nodes[1].id]
    assert body["reviews"][0]["gate"] == 90.0
    assert body["reviews"][0]["overdue"] is False
    # 前序第一个没过门的是概念节点（定性）→ assess
    assert body["next_target"]["action"] == "assess"


async def test_quantitative_weak_point_reports_gap(hub):
    client, app = hub
    path = await _path(hub)
    nodes = await app.state.learning.list_nodes(path["id"])
    await _answer(app, nodes[1].id, score=0.0, count=1)  # 流程节点答错一道

    body = (await client.get(f"/api/v1/learning/paths/{path['id']}")).json()
    assert body["stats"]["weak"] == 1
    weak = body["weak_points"][0]
    assert weak["node_id"] == nodes[1].id
    assert weak["gate"] == 90.0 and weak["gap"] == 90.0 - weak["mastery"]
    assert weak["gate_kind"] == "quantitative"
    assert body["next_target"]["action"] == "assess"  # 概念节点还排在前序前面


async def test_qualitative_weak_point_has_no_score_gap(hub):
    """定性节点作答过没过门也是薄弱点，但它的 gate 记 0、用「离 100 还差多少」当差距。"""
    client, app = hub
    path = await _path(hub, nodes=[{"title": "只此一节", "node_type": "design"}])
    nodes = await app.state.learning.list_nodes(path["id"])
    await _answer(app, nodes[0].id, score=0.0, count=1)

    body = (await client.get(f"/api/v1/learning/paths/{path['id']}")).json()
    weak = body["weak_points"][0]
    assert weak["gate_kind"] == "qualitative"
    assert weak["gate"] == 0.0  # 定性没有分数线（看板按定性样式渲染）
    assert weak["gap"] == 100.0 - weak["mastery"]


async def test_patch_path_and_nodes(hub):
    client, app = hub
    path = await _path(hub)
    nodes = await app.state.learning.list_nodes(path["id"])

    renamed = await client.patch(
        f"/api/v1/learning/paths/{path['id']}", json={"title": "线代 · 第一轮", "summary": "三节"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "线代 · 第一轮"
    assert renamed.json()["summary"] == "三节"

    added = await client.post(
        f"/api/v1/learning/paths/{path['id']}/nodes",
        json={"title": "向量空间", "node_type": "memory", "parent_id": nodes[0].id},
    )
    assert added.status_code == 200
    assert added.json()["depth"] == 1
    assert added.json()["gate"] == 90.0

    updated = await client.patch(
        f"/api/v1/learning/nodes/{nodes[2].id}", json={"node_type": "memory"}
    )
    assert updated.status_code == 200
    assert updated.json()["node_type"] == "memory"
    assert updated.json()["gate_kind"] == "quantitative"

    moved = await client.post(
        f"/api/v1/learning/nodes/{added.json()['id']}/move", json={"direction": "up"}
    )
    assert moved.status_code == 200
    assert [node["title"] for node in moved.json()["nodes"]] == [
        "向量与线性组合",
        "向量空间",
        "矩阵与初等变换",
        "特征值分解",
    ]

    deleted = await client.delete(f"/api/v1/learning/nodes/{nodes[1].id}")
    assert deleted.status_code == 200
    remaining = (await client.get(f"/api/v1/learning/paths/{path['id']}")).json()["nodes"]
    assert [node["title"] for node in remaining] == ["向量与线性组合", "向量空间"]

    gone = await client.delete(f"/api/v1/learning/paths/{path['id']}")
    assert gone.status_code == 200
    assert (await client.get(f"/api/v1/learning/paths/{path['id']}")).status_code == 404


async def test_delete_node_detaches_questions(hub):
    client, app = hub
    path = await _path(hub)
    nodes = await app.state.learning.list_nodes(path["id"])
    question = await app.state.questions.create_question(
        stem="挂在设计节点上的题", options=["1", "2"], answer="A", node_id=nodes[2].id
    )
    assert (await client.delete(f"/api/v1/learning/nodes/{nodes[2].id}")).status_code == 200
    assert (await app.state.questions.get_question(question.id)).node_id is None


async def test_no_advance_endpoint(hub):
    """推进端点已删：门就是游标，下一目标由服务端每回合现算（不是 409，是根本没有）。"""
    client, app = hub
    path = await _path(hub)
    response = await client.post(f"/api/v1/learning/paths/{path['id']}/advance")
    assert response.status_code == 404
    assert "error" not in response.json()  # 连路由都不存在，走的是 FastAPI 默认 404


async def test_start_session_binds_path_without_running_a_turn(hub):
    """点「去聊天」只备会话：绑定路径 + 标题设成路径名，**一个回合都不跑**。

    这是「进路径不再自动打模型」的回归锁。以前这个端点会拿 `session_start` 文案当
    用户消息落库，紧接着起一个 mastery_path 回合——用户一个字没打就先烧一次模型调用。
    现在第一句由用户在输入框里自己打，端点零 LLM。
    """
    client, app = hub
    path = await _path(hub)
    response = await client.post(
        f"/api/v1/learning/paths/{path['id']}/session", json={"language": "zh"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path_id"] == path["id"]
    assert "turn_id" not in body  # 不起回合，就没有回合 id
    session = await app.state.runtime._sessions.get_session(body["session_id"])
    assert session is not None
    assert session.capability == "mastery_path"  # 学习会话跑的必须是学习能力
    assert session.title == path["title"]
    # 绑定落在路径上：会话能反查出路径（能力按会话找路径就靠这个）
    assert (await app.state.learning.get_path_by_session(session.id)).id == path["id"]

    # **没替用户开口**：库里一条消息都没有，也没有回合被登记（LLM 一次都没调）
    assert await app.state.runtime._sessions.list_messages(session.id) == []
    assert app.state.runtime._executions == {}

    # 同一路径再进一次：复用同一个会话，还是不起回合
    again = await client.post(
        f"/api/v1/learning/paths/{path['id']}/session", json={"language": "zh"}
    )
    assert again.status_code == 200, again.text
    assert again.json()["session_id"] == body["session_id"]
    assert app.state.runtime._executions == {}


async def test_questions_filter_by_node(hub):
    client, app = hub
    path = await _path(hub)
    nodes = await app.state.learning.list_nodes(path["id"])
    first = await app.state.questions.create_question(
        stem="向量题", options=["1", "2"], answer="A", node_id=nodes[0].id
    )
    await app.state.questions.create_question(
        stem="矩阵题", options=["1", "2"], answer="A", node_id=nodes[1].id
    )
    response = await client.get(f"/api/v1/questions?node_id={nodes[0].id}")
    assert response.status_code == 200
    rows = response.json()["questions"]
    assert [row["id"] for row in rows] == [first.id]


async def test_error_envelopes(hub):
    client, app = hub
    missing = await client.get("/api/v1/learning/paths/lpath-ghost")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    path = await _path(hub)
    nodes = await app.state.learning.list_nodes(path["id"])
    bad_node = await client.post(f"/api/v1/learning/paths/{path['id']}/nodes", json={"title": "  "})
    assert bad_node.status_code == 422
    assert bad_node.json()["error"]["code"] == "invalid_node"

    bad_move = await client.post(
        f"/api/v1/learning/nodes/{nodes[0].id}/move", json={"direction": "sideways"}
    )
    assert bad_move.status_code == 422
    assert bad_move.json()["error"]["code"] == "invalid_move"

    ghost_move = await client.post(
        "/api/v1/learning/nodes/lnode-ghost/move", json={"direction": "up"}
    )
    assert ghost_move.status_code == 404

    empty_topic = await client.patch(f"/api/v1/learning/paths/{path['id']}", json={"topic": "   "})
    assert empty_topic.status_code == 422
    assert empty_topic.json()["error"]["code"] == "invalid_path"

    ghost_session = await client.post(
        "/api/v1/learning/paths/lpath-ghost/session", json={"language": "zh"}
    )
    assert ghost_session.status_code == 404


# ---- 看板操作三件（对齐上游）：跳过此题 / 重做路径 / 到期复习聚合 ----


async def _pending(app, path_id, node_id, *, turn_id="turn-1") -> str:
    """摆一张在飞的卡，返回它的 question_id（路径绑给发卡的那个会话）。"""
    await app.state.learning.bind_session(path_id, "sess-ui")
    question = await app.state.questions.create_question(
        stem="在飞的题", options=["1", "2"], answer="A", node_id=node_id
    )
    await app.state.learning.open_interaction(
        path_id=path_id,
        node_id=node_id,
        question_id=question.id,
        kind="quiz",
        card_prompt=question.stem,
        session_id="sess-ui",
        turn_id=turn_id,
    )
    return question.id


async def test_skip_question_abandons_the_card_only(hub):
    """跳过：卡作废、下一目标不再被劫持，但**掌握度与作答历史一个字不动**。"""
    client, app = hub
    path = await _path(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(path["id"]))[1]
    await _answer(app, node.id, score=1.0, count=1)
    before = (await learning.get_node(node.id)).mastery
    await _pending(app, path["id"], node.id)

    response = await client.post(f"/api/v1/learning/paths/{path['id']}/skip-question")
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] == 1
    assert body["next_target"]["action"] != "answer_pending"
    assert await learning.pending_interaction(path["id"]) is None
    assert (await learning.get_node(node.id)).mastery == before
    assert before > 0  # 作答历史还在（不是清零）
    # 没有卡在飞时再点一次：skipped=0，不算错误
    again = await client.post(f"/api/v1/learning/paths/{path['id']}/skip-question")
    assert again.status_code == 200 and again.json()["skipped"] == 0


async def test_redo_path_clears_progress_but_keeps_nodes(hub):
    client, app = hub
    path = await _path(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(path["id"])
    await learning.record_qualitative(nodes[0].id, passed=True)
    await _answer(app, nodes[1].id, score=1.0, count=3)
    assert (await learning.get_node(nodes[1].id)).cleared is True

    response = await client.post(f"/api/v1/learning/paths/{path['id']}/redo")
    assert response.status_code == 200
    body = response.json()
    assert body["reset"] == len(nodes)
    assert [node["id"] for node in body["nodes"]] == [node.id for node in nodes]
    assert body["stats"]["mastered"] == 0 and body["stats"]["progress"] == 0.0
    fresh = await learning.get_node(nodes[1].id)
    assert (fresh.mastery, fresh.state, fresh.review_stage) == (0.0, "not_started", 0)
    assert (await learning.get_node(nodes[0].id)).assess_passed is False
    # 题是历史存档：节点上挂的题还在（作答记录清掉了）
    assert await app.state.questions.list_questions(node_id=nodes[1].id) != []


async def test_reviews_aggregate_across_paths(hub):
    """到期复习聚合：一条路径的也在、多条路径的也汇总，每条带上是哪条路径的。"""
    client, app = hub
    path = await _path(hub)
    other = await _path(
        hub, topic="概率论基础", nodes=[{"title": "条件概率", "node_type": "concept"}]
    )
    learning = app.state.learning
    nodes = await learning.list_nodes(path["id"])
    await learning.record_qualitative(nodes[0].id, passed=True)
    other_nodes = await learning.list_nodes(other["id"])
    await learning.record_qualitative(other_nodes[0].id, passed=True)

    response = await client.get("/api/v1/learning/reviews")
    assert response.status_code == 200
    reviews = response.json()["reviews"]
    assert {item["path_id"] for item in reviews} == {path["id"], other["id"]}
    assert {item["path_title"] for item in reviews} == {path["title"], other["title"]}
    assert all(item["node_id"] and item["overdue"] is False for item in reviews)


async def test_skip_and_redo_404_on_ghost_path(hub):
    client, _app = hub
    assert (
        await client.post("/api/v1/learning/paths/lpath-ghost/skip-question")
    ).status_code == 404
    assert (await client.post("/api/v1/learning/paths/lpath-ghost/redo")).status_code == 404
