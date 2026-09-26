"""mastery 工具（§7.5 重做）：九个动作，其中最要紧的是「回合内答题闭环」。

工具是「聊天 → 学习路径」的唯一出口。这里盯六件事：
- 建树 / 列路径 / 切路径 / 脱离（窄幂等：同主题且本会话正绑着才算重复）；
- 出卡闭合：服务端渲染卡片 → 收答复 → 归一 → 判分 → 记作答 → 回写掌握度；
- 选项读不出时**重问一次**，仍读不出就**不判不挂账**（判错会冤枉人，判对更糟）；
- 定性门：只有 concept/design 能 assess，结论落到 assess_passed；
- 未决题的收尾（grade）幂等；
- **答案键不外泄**：status/出卡阶段的输出里不许出现未作答题的答案。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.tools.builtin.mastery_tool import MasteryTool

# 四类型齐活：concept（定性·前序第一）/ procedure（定量·摸底对象）/ memory / design
NODES = [
    {"title": "向量与线性组合", "type": "concept", "description": "入口"},
    {"title": "矩阵与初等变换", "type": "procedure", "parent_index": 0},
    {"title": "特征值分解", "type": "design", "parent_title": "矩阵与初等变换"},
]


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


class AskStub:
    """`ask_user_fn` 的替身：按脚本逐次作答，并记住每次问了什么。"""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.answers.pop(0) if self.answers else ""

    @property
    def last_options(self) -> list[dict]:
        return list(self.calls[-1].get("options") or [])


def _ctx(stub: AskStub | None = None, config=None, session_id="sess-learn", **args) -> ToolContext:
    metadata = {"ask_user_fn": stub} if stub is not None else {}
    return ToolContext(
        turn_id="turn-1",
        session_id=session_id,
        language="zh",
        config=config or {},
        metadata=metadata,
        args={"action": "status", **args},
    )


async def _create(hub, topic="线性代数基础", nodes=None) -> dict:
    _client, app = hub
    result = await MasteryTool().run(_ctx(action="build", topic=topic, nodes=nodes or NODES))
    assert result.ok is True, result.output
    assert result.detail is not None
    return result.detail


async def _seed_questions(app, node_id: str, count: int, *, answer="A", stem="题干") -> list:
    """直接在库里备好题（省掉出题那次 LLM 调用，让用例只盯答题闭环）。"""
    return [
        await app.state.questions.create_question(
            stem=f"{stem} {index + 1}",
            options=["选项一", "选项二"],
            answer=answer,
            explanation="解析文字",
            node_id=node_id,
        )
        for index in range(count)
    ]


# ---- 建、列、切、脱离 ----


async def test_build_then_paths_lists(hub):
    client, app = hub
    detail = await _create(hub)
    path_id = detail["path_id"]
    assert path_id.startswith("lpath-")
    assert detail["created"] is True
    path = await app.state.learning.get_path_model(path_id)
    # 聊天驱动的路径顺手绑上本回合会话，下一轮 status 不用再传 path_id
    assert path is not None and path.session_id == "sess-learn"
    nodes = await app.state.learning.list_nodes(path_id)
    assert [node.title for node in nodes] == ["向量与线性组合", "矩阵与初等变换", "特征值分解"]

    # 窄幂等：同主题**且本会话正绑着它**才原样返回（挡模型重试）
    again = await _create(hub)
    assert again["path_id"] == path_id and again["created"] is False
    assert len((await client.get("/api/v1/learning/paths")).json()["paths"]) == 1

    # 换个主题就是另一条路径（想重学一遍就该能再建一条），会话跟着改绑到新的这条
    other = await _create(hub, topic="概率论基础", nodes=[{"title": "条件概率"}])
    assert other["path_id"] != path_id and other["created"] is True
    assert (await app.state.learning.get_path_model(path_id)).session_id is None

    listing = await MasteryTool().run(_ctx(action="paths"))
    assert listing.ok is True
    assert listing.detail is not None
    assert {row["path_id"] for row in listing.detail["paths"]} == {path_id, other["path_id"]}
    rows = {row["path_id"]: row for row in listing.detail["paths"]}
    # 会话与路径一对一：本会话正绑着哪条，列表上一眼看得出
    assert rows[other["path_id"]]["bound"] is True
    assert rows[path_id]["bound"] is False


async def test_build_rejects_bad_input(hub):
    tool = MasteryTool()
    assert (await tool.run(_ctx(action="build", nodes=NODES))).ok is False
    assert (await tool.run(_ctx(action="build", topic="空"))).ok is False
    bad = await tool.run(_ctx(action="build", topic="坏树", nodes=[{"title": "A"}, "B"]))
    assert bad.ok is False and "节点对象" in bad.output


async def test_switch_and_leave_rebind_session(hub):
    client, app = hub
    first = await _create(hub)
    second = await _create(hub, topic="概率论基础", nodes=[{"title": "条件概率"}])
    learning = app.state.learning
    # switch 之后：本会话绑到第二条（第一条的绑定被顶掉）
    switched = await MasteryTool().run(
        _ctx(action="switch", session_id="sess-learn", path_id=second["path_id"])
    )
    assert switched.ok is True, switched.output
    assert (await learning.get_path_by_session("sess-learn")).id == second["path_id"]
    assert (await learning.get_path_model(first["path_id"])).session_id is None
    # switch 到不存在的路径：拒绝，绑定不动
    ghost = await MasteryTool().run(_ctx(action="switch", path_id="lpath-ghost"))
    assert ghost.ok is False and "不存在" in ghost.output
    assert (await learning.get_path_by_session("sess-learn")).id == second["path_id"]

    # leave：脱离但进度/题目/作答全留
    node = (await learning.list_nodes(second["path_id"]))[0]
    await learning.record_qualitative(node.id, passed=True)
    left = await MasteryTool().run(_ctx(action="leave", session_id="sess-learn"))
    assert left.ok is True and left.detail["left"] is True
    assert await learning.get_path_by_session("sess-learn") is None
    assert (await learning.get_node(node.id)).assess_passed is True


async def test_status_reads_board_numbers(hub):
    client, app = hub
    detail = await _create(hub)
    path_id = detail["path_id"]
    learning = app.state.learning
    nodes = await learning.list_nodes(path_id)
    # 概念节点：讲一遍过门（定性）；流程节点：答错一道 → 薄弱点
    await learning.record_qualitative(nodes[0].id, passed=True)
    await _seed_questions(app, nodes[1].id, 1, stem="矩阵乘法要求什么？")
    question = (await app.state.questions.list_questions(node_id=nodes[1].id))[0]
    await app.state.questions.record_attempt(
        question.id, answer="B", correct=False, score=0.0, source="deterministic"
    )
    await learning.on_attempt(nodes[1].id)

    result = await MasteryTool().run(_ctx(action="status"))
    assert result.ok is True
    assert "矩阵与初等变换" in result.output and "薄弱点" in result.output
    assert "下一目标" in result.output or "下一目标" in (result.detail["next_target"]["reason"])
    # detail 与看板 REST 读同一份聚合（同一个 model_dump，不手抄）
    board = (await client.get(f"/api/v1/learning/paths/{path_id}")).json()
    assert result.detail is not None
    assert result.detail["stats"] == board["stats"]
    assert result.detail["weak_points"] == board["weak_points"]
    assert result.detail["next_target"] == board["next_target"]


# ---- 答题闭环 ----


async def test_quiz_blocks_and_grades(hub):
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[1]  # procedure：定量门 90
    _questions = await _seed_questions(app, node.id, 3, answer="B", stem="矩阵乘法")

    stub = AskStub("B")
    result = await MasteryTool().run(_ctx(stub, action="quiz", node_id=node.id, author=3, cards=1))
    assert result.ok is True, result.output
    # 卡片由服务端渲染：选项是题库原文，带 A/B 标签
    assert [item["label"] for item in stub.last_options] == ["A", "B"]
    assert stub.last_options[0]["description"] == "选项一"
    assert "第 1/1 题" in stub.calls[-1]["context"]

    card = result.detail["cards"][0]
    assert card["answered"] is True
    assert card["grading"]["correct"] is True and card["grading"]["source"] == "deterministic"
    assert card["answer"] == "B"  # 归一成标签（用户可能点选项，也可能写「选 B」）
    assert card["question"]["answer"] == "B"  # 判分之后才给答案键
    # 作答落进题库、掌握度回写节点、交互落定
    attempts = await app.state.questions.list_attempts(question_id=card["question"]["id"])
    assert len(attempts) == 1 and attempts[0].correct is True
    fresh = await learning.get_node(node.id)
    assert fresh.mastery == 50.0  # 一道题答对：证据封顶 50
    assert fresh.last_practiced_at is not None
    interaction = await learning.get_interaction(card["interaction_id"])
    assert interaction.status == "graded" and interaction.correct is True
    assert await learning.pending_interaction(detail["path_id"]) is None

    # 出卡时 cards=1 但一次流程只消耗这一张：再跑一次会换下一道（未作答优先）
    second = await MasteryTool().run(_ctx(AskStub("A"), action="quiz", node_id=node.id, author=3))
    assert second.ok is True
    assert second.detail["cards"][0]["question"]["id"] != card["question"]["id"]


async def test_quiz_unreadable_answer_reasks_once(hub):
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[1]
    await _seed_questions(app, node.id, 3, answer="B")

    # 第一次答复读不出选项 → 重问一次；第二次给出标签 → 正常判分
    stub = AskStub("嗯……那个啥", "B")
    result = await MasteryTool().run(_ctx(stub, action="quiz", node_id=node.id, author=3, cards=1))
    assert result.ok is True, result.output
    assert len(stub.calls) == 2
    assert "没能读出" in stub.calls[1]["question"]  # 重问用的是 quiz.ask_clarify
    assert result.detail["cards"][0]["grading"]["correct"] is True


async def test_quiz_gives_up_without_grading(hub):
    """两次都读不出就**不判不挂账**：掌握度不动、没有作答行、交互标 abandoned。"""
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[1]
    await _seed_questions(app, node.id, 3, answer="B")

    stub = AskStub("嗯……", "还是没想好")
    result = await MasteryTool().run(_ctx(stub, action="quiz", node_id=node.id, author=3, cards=1))
    assert result.ok is True, result.output
    assert len(stub.calls) == 2  # 只重问一次，不无限纠缠
    card = result.detail["cards"][0]
    assert card["answered"] is False and "读不出" in card["note"]
    assert (await learning.get_node(node.id)).mastery == 0.0
    assert await app.state.questions.list_attempts(question_id=card["question"]["id"]) == []
    interaction = await learning.get_interaction(card["interaction_id"])
    assert interaction.status == "abandoned" and interaction.grade_source == "unreadable"
    # 不挂账 = 不留未决：下一目标照常往前走
    assert await learning.pending_interaction(detail["path_id"]) is None


async def test_quiz_empty_answer_leaves_interaction_pending(hub):
    """用户没作答（超时/跳过）：留着未决行，下一回合 answer_pending 优先重提。"""
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[1]
    questions = await _seed_questions(app, node.id, 3, answer="B")

    result = await MasteryTool().run(
        _ctx(AskStub(""), action="quiz", node_id=node.id, author=3, cards=1)
    )
    assert result.ok is True, result.output
    assert result.detail["cards"][0]["answered"] is False
    pending = await learning.pending_interaction(detail["path_id"])
    assert pending is not None and pending.question_id in {question.id for question in questions}
    assert (await learning.next_objective(detail["path_id"])).action == "answer_pending"


async def test_answer_key_never_leaks_before_grading(hub):
    """答案键不外泄：出卡阶段与只读动作的输出里都不许出现未作答题的答案。"""
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[1]
    await _seed_questions(app, node.id, 3, answer="B", stem="矩阵乘法")

    # ① 只读动作：status 只说数字与标题，连题干都不给
    status = await MasteryTool().run(_ctx(action="status"))
    assert status.ok is True
    assert "解析文字" not in status.output
    assert all("answer" not in node_ for node_ in status.detail["nodes"])

    # ② 出卡阶段：第一次答复读不出（不判分），这一路的输出与 detail 里都不带答案键
    stub = AskStub("嗯……", "还是没想好")
    leaked = await MasteryTool().run(_ctx(stub, action="quiz", node_id=node.id, author=3, cards=1))
    assert leaked.ok is True
    assert "解析文字" not in leaked.output
    card = leaked.detail["cards"][0]
    assert "answer" not in card["question"]
    assert "explanation" not in card["question"]
    # 卡片（模型与前端看到的）里也只有题干与选项
    assert "解析文字" not in str(stub.calls[0])


async def test_probe_only_for_untouched_quantitative_node(hub):
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    concept, procedure = nodes[0], nodes[1]

    # 定性节点没有摸底这一步
    refused = await MasteryTool().run(_ctx(AskStub(""), action="probe", node_id=concept.id))
    assert refused.ok is False and "定性节点" in refused.output

    await _seed_questions(app, procedure.id, 3, answer="B", stem="矩阵乘法")
    stub = AskStub("B", "B")
    probed = await MasteryTool().run(_ctx(stub, action="probe", node_id=procedure.id, cards=2))
    assert probed.ok is True, probed.output
    assert len(probed.detail["cards"]) == 2
    assert (await learning.get_node(procedure.id)).mastery == 80.0  # 两题封顶 80，还没过门

    # 碰过一次就不再叫摸底了（下一题该走 quiz）
    again = await MasteryTool().run(_ctx(AskStub("B"), action="probe", node_id=procedure.id))
    assert again.ok is False and "不是没碰过的节点" in again.output

    # 第三题答对 → 过 90 的门（门要两次以上证据，这条是刻意设计）
    third = await MasteryTool().run(
        _ctx(AskStub("B"), action="quiz", node_id=procedure.id, author=3, cards=1)
    )
    assert third.ok is True
    assert (await learning.get_node(procedure.id)).cleared is True
    assert third.detail["cleared"] is True


# ---- 定性门 ----


async def test_assess_requires_qualitative_node(hub):
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    refused = await MasteryTool().run(_ctx(AskStub("讲解"), action="assess", node_id=nodes[1].id))
    assert refused.ok is False and "定量节点" in refused.output


async def test_assess_pass_writes_verdict(hub):
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[0]  # concept
    install_scripted(
        lambda: ScriptedLLM(
            [ScriptedStep(chunks=['{"passed": true, "feedback": "定义与直观都讲到了。"}'])]
        )
    )
    stub = AskStub("线性组合就是把向量按标量加起来……")
    result = await MasteryTool().run(_ctx(stub, action="assess", node_id=node.id))
    assert result.ok is True, result.output
    # 纯文本卡：没有选项，允许自由作答
    assert stub.calls[-1]["options"] == []
    assert stub.calls[-1]["allow_free_text"] is True

    fresh = await learning.get_node(node.id)
    assert fresh.assess_passed is True and fresh.assessed_at is not None
    assert fresh.mastery == 100.0 and fresh.cleared is True
    card = result.detail["cards"][0]
    assert card["grading"]["correct"] is True and card["grading"]["source"] == "assessor"
    assert card["question"]["type"] == "assess"
    interaction = await learning.get_interaction(card["interaction_id"])
    assert interaction.status == "graded" and interaction.grade_source == "assessor"


async def test_assess_fails_closed_on_bad_reply(hub):
    """评定回复读不出结论 → 一律判不过（fail-closed），掌握度封在 40。"""
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[0]
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["我觉得还行吧"])]))
    result = await MasteryTool().run(_ctx(AskStub("大概是这样"), action="assess", node_id=node.id))
    assert result.ok is True, result.output
    fresh = await learning.get_node(node.id)
    assert fresh.assess_passed is False and fresh.cleared is False
    # 展示分是 min(证据值, 40)：这个节点一道题都没做过，证据 0 ⇒ 0 分（封顶只挡虚高）
    assert fresh.mastery == 0.0


# ---- grade：未决题的收尾 ----


async def test_grade_closes_pending_and_is_idempotent(hub):
    client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[1]
    questions = await _seed_questions(app, node.id, 3, answer="B")
    # 先用「没作答」留下一条未决交互
    await MasteryTool().run(_ctx(AskStub(""), action="quiz", node_id=node.id, author=3, cards=1))
    pending = await learning.pending_interaction(detail["path_id"])
    assert pending is not None and pending.question_id in {question.id for question in questions}

    graded = await MasteryTool().run(_ctx(action="grade", answer="选 B"))
    assert graded.ok is True, graded.output
    assert graded.detail["cards"][0]["grading"]["correct"] is True
    assert await learning.pending_interaction(detail["path_id"]) is None
    assert (await learning.get_node(node.id)).mastery == 50.0

    # 幂等：已经判过的行再调一次是回放，不会重复记作答
    replay = await MasteryTool().run(_ctx(action="grade", interaction_id=pending.id, answer="选 A"))
    assert replay.ok is True and replay.detail["replayed"] is True
    attempts = await app.state.questions.list_attempts(question_id=pending.question_id)
    assert len(attempts) == 1


async def test_grade_without_pending(hub):
    client, app = hub
    await _create(hub)
    result = await MasteryTool().run(_ctx(action="grade", answer="A"))
    assert result.ok is False and "没有未决的题" in result.output


async def test_status_requires_path_and_lists_existing(hub):
    client, app = hub
    tool = MasteryTool()
    # 未知动作
    assert (await tool.run(_ctx(action="upgrade"))).ok is False
    # 明确给了不存在的 path_id
    missing = await tool.run(_ctx(action="status", path_id="lpath-ghost"))
    assert missing.ok is False and "不存在" in missing.output
    # 没绑路径但库里已有别的路径：如实列出来（模型看不到路径表，只能靠这里给）
    await _create(hub)
    unbound = await tool.run(_ctx(action="status", session_id="sess-nowhere"))
    assert unbound.ok is False
    assert "还没绑学习路径" in unbound.output
    assert "线性代数基础" in unbound.output and "switch" in unbound.output
