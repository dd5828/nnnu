"""mastery 八工具（§7.5）：拆开后每个工具只干一件事，这里按「谁负责什么」分四组盯。

工具是「聊天 → 学习路径」的唯一出口。八工具与旧九动作相比，最要紧的两处换手：
- **出题权归模型**：`mastery_quiz` 只登记（答案留服务端），发卡是模型自己调 `ask_user`
  （第二段，落在 `ask_user` 缝里，见 test_mastery_seam），`mastery_grade` 才判分；
- **定性门归模型**：`mastery_assess(passed=…)` 直接把结论落库，不再有独立 LLM 评定器。

本文件盯：
- 建树 / 列路径 / 切路径 / 脱离（窄幂等：同主题且本会话正绑着才算重复）；
- `mastery_quiz` 登记：答案键按入库口径归一、题面脱敏（答案键与解析不回给模型）、
  题目类型与节点门适配（定性节点拒收）；
- `mastery_grade` 判分：**只认服务端落库的原话**（模型转述无效）、幂等回放、
  选项读不出时拒绝判分（判错会冤枉人，判对更糟）、没作答就不判不挂账；
- `mastery_assess` 定性门：只有 concept/design 能评、结论落到 `assess_passed`、
  原话优先取 `ask_user` 缝压在 metadata 上的那份。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.core.tool_protocol import ToolContext
from nnnu.tools.builtin.mastery import (
    MASTERY_TOOLS,
    MasteryAssessTool,
    MasteryBuildTool,
    MasteryGradeTool,
    MasteryLeaveTool,
    MasteryPathsTool,
    MasteryQuizTool,
    MasteryStatusTool,
    MasterySwitchTool,
)
from nnnu.tools.builtin.mastery.common import ANSWER_KEY

# 四类型齐活：concept（定性·前序第一）/ procedure（定量·摸底对象）/ design（定性）
NODES = [
    {"title": "向量与线性组合", "type": "concept", "description": "入口"},
    {"title": "矩阵与初等变换", "type": "procedure", "parent_index": 0},
    {"title": "特征值分解", "type": "design", "parent_title": "矩阵与初等变换"},
]

# 模型出题时给的一套选项与答案（单选）
Q_OPTIONS = ["不变", "变成 3 倍", "变成 1/3", "变成 9 倍"]
Q_ANSWER = "B"


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


def _ctx(config=None, session_id="sess-learn", **args) -> ToolContext:
    return ToolContext(
        turn_id="turn-1",
        session_id=session_id,
        language="zh",
        config=config or {},
        metadata={},
        args=args,
    )


def _ctx_with_answer(spoken: str, **args) -> ToolContext:
    """带 `ask_user` 缝留下的原话的上下文（定性门取它当原话）。"""
    ctx = _ctx(**args)
    ctx.metadata[ANSWER_KEY] = spoken
    return ctx


async def _create(hub, topic="线性代数基础", nodes=None) -> dict:
    _client, app = hub
    result = await MasteryBuildTool().run(_ctx(topic=topic, nodes=nodes or NODES))
    assert result.ok is True, result.output
    assert result.detail is not None
    return result.detail


async def _register(app, path_id, node, *, answer=Q_ANSWER, stem="矩阵乘法的行×列是什么？") -> dict:
    """走真实第一段：模型出题 → 登记入库 → 未决交互（`mastery_grade` 要的就是这一行）。"""
    result = await MasteryQuizTool().run(
        _ctx(
            node_id=node.id,
            question=stem,
            expected_answer=answer,
            question_type="single",
            options=Q_OPTIONS,
            explanation="解析文字",
            difficulty="easy",
        )
    )
    assert result.ok is True, result.output
    return result.detail


async def _answer(app, path_id, text, *, session_id="sess-learn", turn_id="turn-1") -> None:
    """走真实第二段的落库那一步（`ask_user` 缝干的活，见 test_mastery_seam）。"""
    recorded = await app.state.learning.record_question_answer(
        path_id, user_answer=text, session_id=session_id, turn_id=turn_id
    )
    assert recorded is not None


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

    listing = await MasteryPathsTool().run(_ctx())
    assert listing.ok is True
    assert listing.detail is not None
    assert {row["path_id"] for row in listing.detail["paths"]} == {path_id, other["path_id"]}
    rows = {row["path_id"]: row for row in listing.detail["paths"]}
    # 会话与路径一对一：本会话正绑着哪条，列表上一眼看得出
    assert rows[other["path_id"]]["bound"] is True
    assert rows[path_id]["bound"] is False


async def test_paths_empty_is_not_an_error(hub):
    """一条路径都没有是正常状态：ok=True 指路，别标成调用出错（模型会当故障重试）。"""
    result = await MasteryPathsTool().run(_ctx())
    assert result.ok is True and result.detail == {"paths": []}
    assert "mastery_build" in result.output


async def test_build_rejects_bad_input(hub):
    tool = MasteryBuildTool()
    assert (await tool.run(_ctx(nodes=NODES))).ok is False
    assert (await tool.run(_ctx(topic="空"))).ok is False
    bad = await tool.run(_ctx(topic="坏树", nodes=[{"title": "A"}, "B"]))
    assert bad.ok is False and "节点对象" in bad.output


async def test_switch_and_leave_rebind_session(hub):
    client, app = hub
    first = await _create(hub)
    second = await _create(hub, topic="概率论基础", nodes=[{"title": "条件概率"}])
    learning = app.state.learning
    # switch 之后：本会话绑到第二条（第一条的绑定被顶掉）
    switched = await MasterySwitchTool().run(
        _ctx(session_id="sess-learn", path_id=second["path_id"])
    )
    assert switched.ok is True, switched.output
    assert (await learning.get_path_by_session("sess-learn")).id == second["path_id"]
    assert (await learning.get_path_model(first["path_id"])).session_id is None
    # switch 到不存在的路径：拒绝，绑定不动
    ghost = await MasterySwitchTool().run(_ctx(path_id="lpath-ghost"))
    assert ghost.ok is False and "不存在" in ghost.output
    assert (await learning.get_path_by_session("sess-learn")).id == second["path_id"]

    # leave：脱离但进度/题目/作答全留
    node = (await learning.list_nodes(second["path_id"]))[0]
    await learning.record_qualitative(node.id, passed=True)
    left = await MasteryLeaveTool().run(_ctx(session_id="sess-learn"))
    assert left.ok is True and left.detail["left"] is True
    assert await learning.get_path_by_session("sess-learn") is None
    assert (await learning.get_node(node.id)).assess_passed is True


# ---- status：只读状态 ----


async def test_status_reads_board_numbers(hub):
    client, app = hub
    detail = await _create(hub)
    path_id = detail["path_id"]
    learning = app.state.learning
    nodes = await learning.list_nodes(path_id)
    # 概念节点：讲一遍过门（定性）；流程节点：答错一道 → 薄弱点
    await learning.record_qualitative(nodes[0].id, passed=True)
    question = await app.state.questions.create_question(
        stem="矩阵乘法要求什么？", options=["选项一", "选项二"], answer="B", node_id=nodes[1].id
    )
    await app.state.questions.record_attempt(
        question.id, answer="B", correct=False, score=0.0, source="deterministic"
    )
    await learning.on_attempt(nodes[1].id)

    result = await MasteryStatusTool().run(_ctx())
    assert result.ok is True
    assert "矩阵与初等变换" in result.output and "薄弱点" in result.output
    assert "下一目标" in result.output or "下一目标" in (result.detail["next_target"]["reason"])
    # detail 与看板 REST 读同一份聚合（同一个 model_dump，不手抄）
    board = (await client.get(f"/api/v1/learning/paths/{path_id}")).json()
    assert result.detail is not None
    assert result.detail["stats"] == board["stats"]
    assert result.detail["weak_points"] == board["weak_points"]
    assert result.detail["next_target"] == board["next_target"]


async def test_status_without_path_still_ok(hub):
    """没绑路径是正常状态（还没建树），不是调用出错：照样 ok=True，把已有的列出来。"""
    tool = MasteryStatusTool()
    empty = await tool.run(_ctx())
    assert empty.ok is True and "还没绑学习路径" in empty.output
    assert empty.detail is None  # 没有路径就没有可报的状态，不硬凑一个

    await _create(hub)
    # 另一个会话没绑任何路径：如实列出库里已有的（模型看不到路径表，只能靠这里给）
    unbound = await tool.run(_ctx(session_id="sess-nowhere"))
    assert unbound.ok is True
    assert "线性代数基础" in unbound.output and "mastery_switch" in unbound.output

    # config 指名的路径不存在：回落成「没绑」而不是报错
    # （绑定的路径优先，所以这里必须用没绑的会话，否则 config 根本轮不上）
    ghost = await tool.run(_ctx(session_id="sess-nowhere", config={"path_id": "lpath-ghost"}))
    assert ghost.ok is True and "还没绑学习路径" in ghost.output


async def test_status_reports_pending_card(hub):
    """未决题要在 status 里报出来（模型靠它知道「先把这张卡答完」）。"""
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    await _register(app, detail["path_id"], nodes[1])

    result = await MasteryStatusTool().run(_ctx())
    assert result.ok is True
    pending = result.detail["pending"]
    assert pending is not None and pending["answered"] is False
    assert pending["node_id"] == nodes[1].id


# ---- mastery_quiz：登记（第一段） ----


async def test_quiz_registers_without_leaking_answer(hub):
    """登记只回题面与脱敏 payload：答案键与解析留在服务端，判分那一刻才出现。"""
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    node = nodes[1]  # procedure：定量门 90

    result = await MasteryQuizTool().run(
        _ctx(
            node_id=node.id,
            question="矩阵乘法的行×列是什么？",
            expected_answer="B",
            question_type="single",
            options=Q_OPTIONS,
            explanation="解析文字",
            difficulty="easy",
        )
    )
    assert result.ok is True, result.output
    payload = result.detail
    assert payload["action"] == "quiz" and payload["status"] == "registered"
    assert payload["node"]["id"] == node.id
    assert payload["options"] == Q_OPTIONS
    # 答案键与解析：输出与 detail 里一个字都不许有
    assert "解析文字" not in result.output
    assert "expected_answer" not in result.output and "expected_answer" not in payload
    assert "explanation" not in payload

    # 转手给 ask_user 的形状：题面 + 带标签的选项 + 节点小字（不是「第几题」）
    ask = payload["ask_user"]
    assert ask["question"] == "矩阵乘法的行×列是什么？"
    assert [item["label"] for item in ask["options"]] == ["A", "B", "C", "D"]
    assert ask["options"][0]["description"] == Q_OPTIONS[0]
    assert ask["question_id"] == payload["question_id"]
    assert ask["context"] == f"节点《{node.title}》"

    # 题目落库当存档：答案键按入库口径存标签，题挂在节点上
    question = await app.state.questions.get_question(payload["question_id"])
    assert question is not None
    assert question.answer == "B" and question.node_id == node.id
    assert question.knowledge_point == node.title and question.source == "mastery"
    # 未决交互已就位（`ask_user` 缝与 `mastery_grade` 都靠它定位）
    pending = await app.state.learning.pending_interaction(detail["path_id"])
    assert pending is not None
    assert pending.question_id == question.id and pending.node_id == node.id
    assert pending.status == "awaiting_input" and pending.kind == "quiz"


async def test_quiz_normalizes_expected_answer(hub):
    """模型习惯写选项原文，库里存标签——归一在读不出时**拒绝登记**（免得定错答案键）。"""
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    node = nodes[1]
    tool = MasteryQuizTool()

    text = await tool.run(
        _ctx(
            node_id=node.id,
            question="倍数问题",
            expected_answer="变成 3 倍",
            options=Q_OPTIONS,
        )
    )
    assert text.ok is True, text.output
    question = await app.state.questions.get_question(text.detail["question_id"])
    assert question.answer == "B"

    vague = await tool.run(
        _ctx(
            node_id=node.id,
            question="倍数问题",
            expected_answer="大概是第三个吧",
            options=Q_OPTIONS,
        )
    )
    assert vague.ok is False and "读不出唯一答案" in vague.output


async def test_quiz_rejects_bad_input(hub):
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    node = nodes[1]
    tool = MasteryQuizTool()
    base = {"node_id": node.id, "options": Q_OPTIONS}

    assert (await tool.run(_ctx(**base))).ok is False  # 缺 question/expected_answer
    assert (await tool.run(_ctx(question="题", **base))).ok is False
    assert (
        await tool.run(_ctx(**base, question="题", expected_answer="B", question_type="填空"))
    ).ok is False
    single = await tool.run(
        _ctx(node_id=node.id, question="题", expected_answer="A", question_type="single")
    )
    assert single.ok is False and "两个选项" in single.output
    short = await tool.run(
        _ctx(
            node_id=node.id,
            question="题",
            expected_answer="答案",
            question_type="short",
            options=Q_OPTIONS,
        )
    )
    assert short.ok is False and "不要给 options" in short.output


async def test_quiz_refuses_qualitative_node(hub):
    """定性节点没有做题这一步：门在讲解判定上，该走 mastery_assess。"""
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    result = await MasteryQuizTool().run(
        _ctx(node_id=nodes[0].id, question="题", expected_answer="B", options=Q_OPTIONS)
    )
    assert result.ok is False and "定性节点" in result.output


async def test_quiz_short_answer_needs_no_options(hub):
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    result = await MasteryQuizTool().run(
        _ctx(
            node_id=nodes[1].id,
            question="说说矩阵乘法的定义",
            expected_answer="左矩阵的行点乘右矩阵的列",
            question_type="short",
        )
    )
    assert result.ok is True, result.output
    assert result.detail["ask_user"]["options"] == []
    question = await app.state.questions.get_question(result.detail["question_id"])
    assert question.type == "short" and question.answer == "左矩阵的行点乘右矩阵的列"


# ---- mastery_grade：判分（第三段） ----


async def test_grade_uses_stored_answer_not_model_transcript(hub):
    """判分只认服务端落库的原话：库里是 B，模型转述成 A 也没用。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    node = nodes[1]
    registered = await _register(app, detail["path_id"], node)
    await _answer(app, detail["path_id"], "B")

    graded = await MasteryGradeTool().run(_ctx(question_id=registered["question_id"], answer="A"))
    assert graded.ok is True, graded.output
    card = graded.detail["cards"][0]
    assert card["answer"] == "B"
    assert card["grading"]["correct"] is True and card["grading"]["source"] == "deterministic"
    assert card["question"]["answer"] == "B"  # 判分之后才给答案键
    assert card["mastery"] == 50.0 and card["cleared"] is False

    attempts = await app.state.questions.list_attempts(question_id=registered["question_id"])
    assert len(attempts) == 1 and attempts[0].answer == "B" and attempts[0].correct is True
    fresh = await learning.get_node(node.id)
    assert fresh.mastery == 50.0 and fresh.last_practiced_at is not None
    interaction = await learning.get_interaction(card["interaction_id"])
    assert interaction.status == "graded" and interaction.correct is True
    assert await learning.pending_interaction(detail["path_id"]) is None


async def test_grade_wrong_answer_keeps_mastery_at_zero(hub):
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    registered = await _register(app, detail["path_id"], nodes[1])
    await _answer(app, detail["path_id"], "C")

    graded = await MasteryGradeTool().run(_ctx())
    assert graded.ok is True, graded.output
    card = graded.detail["cards"][0]
    assert card["grading"]["correct"] is False and card["answer"] == "C"
    assert (await app.state.learning.get_node(nodes[1].id)).mastery == 0.0
    assert registered["question_id"] == card["question"]["id"]


async def test_grade_falls_back_to_arg_when_nothing_recorded(hub):
    """卡超时/回合被停：库里没有原话，answer 参数兜底——但落了库就不许它覆盖。"""
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    await _register(app, detail["path_id"], nodes[1])

    graded = await MasteryGradeTool().run(_ctx(answer="B"))
    assert graded.ok is True, graded.output
    interaction = await app.state.learning.get_interaction(
        graded.detail["cards"][0]["interaction_id"]
    )
    assert interaction.user_answer == "B" and interaction.correct is True


async def test_grade_without_pending(hub):
    _client, app = hub
    await _create(hub)
    result = await MasteryGradeTool().run(_ctx(answer="A"))
    assert result.ok is False and "没有未决的题" in result.output


async def test_grade_is_idempotent(hub):
    """已经判过的题再调一次是回放（模型重试/追问），不重复记作答。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    registered = await _register(app, detail["path_id"], nodes[1])
    await _answer(app, detail["path_id"], "B")
    graded = await MasteryGradeTool().run(_ctx())
    assert graded.ok is True

    replay = await MasteryGradeTool().run(_ctx(question_id=registered["question_id"], answer="A"))
    assert replay.ok is True and replay.detail["replayed"] is True
    assert replay.detail["cards"][0]["answer"] == "B"
    assert (await learning.get_node(nodes[1].id)).mastery == 50.0
    attempts = await app.state.questions.list_attempts(question_id=registered["question_id"])
    assert len(attempts) == 1


async def test_grade_refuses_unreadable_answer(hub):
    """答复读不出选项就不判：留未决行（模型可以再问一遍），掌握度一动不动。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    registered = await _register(app, detail["path_id"], nodes[1])
    await _answer(app, detail["path_id"], "嗯……那个啥")

    result = await MasteryGradeTool().run(_ctx())
    assert result.ok is False and "没能读出" in result.output
    assert (await learning.get_node(nodes[1].id)).mastery == 0.0
    assert await app.state.questions.list_attempts(question_id=registered["question_id"]) == []
    pending = await learning.pending_interaction(detail["path_id"])
    assert pending is not None and pending.status == "awaiting_input"


async def test_grade_empty_answer_leaves_interaction_pending(hub):
    """用户没作答（超时/跳过）：未决行留着，下一回合 answer_pending 优先重提。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    await _register(app, detail["path_id"], nodes[1])

    result = await MasteryGradeTool().run(_ctx())
    assert result.ok is False and "还没收到这一题的作答" in result.output
    pending = await learning.pending_interaction(detail["path_id"])
    assert pending is not None and pending.user_answer == ""
    assert (await learning.next_objective(detail["path_id"])).action == "answer_pending"


# ---- mastery_assess：定性门 ----


async def test_assess_requires_qualitative_node(hub):
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    refused = await MasteryAssessTool().run(_ctx(node_id=nodes[1].id, passed=True))
    assert refused.ok is False and "定量节点" in refused.output


async def test_assess_requires_bool_verdict(hub):
    _client, app = hub
    detail = await _create(hub)
    nodes = await app.state.learning.list_nodes(detail["path_id"])
    tool = MasteryAssessTool()
    assert (await tool.run(_ctx(node_id=nodes[0].id))).ok is False
    assert (await tool.run(_ctx(node_id=nodes[0].id, passed="true"))).ok is False


async def test_assess_pass_writes_verdict(hub):
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[0]  # concept

    result = await MasteryAssessTool().run(
        _ctx_with_answer(
            "线性组合就是把向量按标量加起来……",
            node_id=node.id,
            passed=True,
            feedback="定义与直观都讲到了。",
        )
    )
    assert result.ok is True, result.output
    fresh = await learning.get_node(node.id)
    assert fresh.assess_passed is True and fresh.assessed_at is not None
    assert fresh.cleared is True
    card = result.detail["cards"][0]
    assert card["grading"]["correct"] is True and card["grading"]["source"] == "model"
    assert card["question"]["type"] == "assess" and card["question"]["options"] == []
    assert card["answer"] == "线性组合就是把向量按标量加起来……"
    interaction = await learning.get_interaction(card["interaction_id"])
    assert interaction.status == "graded" and interaction.grade_source == "model"
    assert interaction.user_answer == "线性组合就是把向量按标量加起来……"
    assert await learning.pending_interaction(detail["path_id"]) is None


async def test_assess_prefers_seam_answer_over_arg(hub):
    """原话优先：`ask_user` 缝压在 metadata 上的那份盖过模型转述的 answer 参数。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[0]

    result = await MasteryAssessTool().run(
        _ctx_with_answer("学习者自己打的字", node_id=node.id, passed=True, answer="模型转述的版本")
    )
    assert result.ok is True, result.output
    interaction = await learning.get_interaction(result.detail["cards"][0]["interaction_id"])
    assert interaction.user_answer == "学习者自己打的字"


async def test_assess_fail_keeps_node_uncleared(hub):
    """讲得不到位就不过：`assess_passed` 落假，节点没过门（宁可让人再讲一遍）。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[0]

    result = await MasteryAssessTool().run(
        _ctx_with_answer("大概是这样吧", node_id=node.id, passed=False, feedback="再讲讲为什么")
    )
    assert result.ok is True, result.output
    fresh = await learning.get_node(node.id)
    assert fresh.assess_passed is False and fresh.cleared is False
    # 展示分是 min(证据值, 40)：这个节点一道题都没做过，证据 0 ⇒ 0 分（封顶只挡虚高）
    assert fresh.mastery == 0.0
    assert result.detail["cards"][0]["grading"]["correct"] is False
    # 判过就是判过（未决清空），下一目标往前走而不是卡在这张卡上
    assert await learning.pending_interaction(detail["path_id"]) is None


async def test_assess_abandons_card_from_other_node(hub):
    """换节点了：旧卡作废，免得下一轮又被当成「未决的那道」提出来。"""
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    nodes = await learning.list_nodes(detail["path_id"])
    await _register(app, detail["path_id"], nodes[1])  # 定量节点的卡在飞

    result = await MasteryAssessTool().run(
        _ctx_with_answer("讲一遍", node_id=nodes[0].id, passed=True)
    )
    assert result.ok is True, result.output
    assert await learning.pending_interaction(detail["path_id"]) is None
    assert (await learning.get_node(nodes[0].id)).assess_passed is True


# ---- 工具面本身 ----


def test_tool_surface_is_eight_read_grade_write_tools():
    """八个工具、名字不重、零 LLM（定性判定归模型后服务端不再调模型）。"""
    names = [tool_class.definition.name for tool_class in MASTERY_TOOLS]
    assert names == [
        "mastery_status",
        "mastery_quiz",
        "mastery_grade",
        "mastery_assess",
        "mastery_build",
        "mastery_paths",
        "mastery_switch",
        "mastery_leave",
    ]
    assert len(set(names)) == 8
    # probe 只剩 `next.action` 的一个取值，不再是工具
    assert "mastery_probe" not in names and "probe" not in names


async def test_grade_refuses_qualitative_card_and_keeps_it(hub):
    """定性门的卡别拿去判分：指路 `mastery_assess`，而且**别把卡作废掉**。

    真机踩过：模型对着「讲一遍」的卡调 `mastery_grade`，被当成「题目已被删除」作废，
    卡没了、用户的讲解也没处可落，模型就来回出题判分转圈。
    """
    _client, app = hub
    detail = await _create(hub)
    learning = app.state.learning
    node = (await learning.list_nodes(detail["path_id"]))[0]  # concept
    interaction = await learning.open_interaction(
        path_id=detail["path_id"],
        node_id=node.id,
        kind="assess",
        card_prompt=node.title,
        session_id="sess-learn",
        turn_id="turn-1",
    )

    refused = await MasteryGradeTool().run(_ctx())
    assert refused.ok is False
    assert "mastery_assess" in refused.output
    fresh = await learning.get_interaction(interaction.id)
    assert fresh.status == "awaiting_input"  # 卡还在飞：等用户开口讲，才轮到评定
