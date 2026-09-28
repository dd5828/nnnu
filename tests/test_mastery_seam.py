"""`ask_user` 缝（`capabilities/mastery/ask_seam.py`）：卡面归位 + 学习者原话落库。

出题权交给模型之后，「登记的那道题」与「发出去的那道题」之间、以及「用户答的话」与
「模型转述的作答」之间就有了缝。本模块在 `ask_user_fn` 外面包一层把两个口子堵上，
这里盯四件事：
- 发卡前**卡面按库里那道题重写**（模型在 ask_user 里塞别的题面不作数，选项/小字也重来）；
- 发卡后**原话先落库再回给模型**（`mastery_grade` 只认那一列），并压在 metadata 上；
- 没绑路径 / 没未决题 / 定性门的空 question_id：一律原样放行；
- 落库这步是 best-effort：空作答跳过、抛异常也照样把答复还给模型。

真实的收发（走 WS 的暂停点）在 test_learning_pipeline 的金路径里；这里只喂替身。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.capabilities.mastery.ask_seam import install
from nnnu.core.context import SessionRef, UnifiedContext
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import ToolContext
from nnnu.services.sessions.models import Message
from nnnu.tools.builtin.mastery import MasteryBuildTool, MasteryQuizTool
from nnnu.tools.builtin.mastery.common import ANSWER_KEY

NODES = [
    {"title": "向量与线性组合", "type": "concept"},
    {"title": "矩阵与初等变换", "type": "procedure", "parent_index": 0},
]

TOPIC = "线性代数基础"
STEM = "矩阵乘法的行×列是什么？"
OPTIONS = ["不变", "变成 3 倍", "变成 1/3"]


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


class Inner:
    """内层 `ask_user_fn` 的替身：记下收到的卡面，按脚本作答。"""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def __call__(self, question, options, ask_id, *, allow_free_text=False, context=""):
        self.calls.append(
            {
                "question": question,
                "options": options,
                "ask_id": ask_id,
                "allow_free_text": allow_free_text,
                "context": context,
            }
        )
        return self.answers.pop(0) if self.answers else ""

    @property
    def last(self) -> dict:
        return self.calls[-1]


def _ctx(inner: Inner, *, session_id="sess-learn", config=None, path_id="") -> UnifiedContext:
    return UnifiedContext(
        session=SessionRef(id=session_id),
        capability="mastery_path",
        message=Message.new(session_id=session_id, role="user", content="接着学"),
        config={"path_id": path_id} if path_id else (config or {}),
        language="zh",
        metadata={"ask_user_fn": inner},
    )


async def _register(app, *, node_index=1) -> dict:
    """把一个会话 + 一条路径 + 一道已登记的题摆好（三段式的第一段）。"""
    built = await MasteryBuildTool().run(
        ToolContext(
            turn_id="turn-1",
            session_id="sess-learn",
            language="zh",
            config={},
            metadata={},
            args={"topic": TOPIC, "nodes": NODES},
        )
    )
    assert built.ok is True, built.output
    path_id = built.detail["path_id"]
    nodes = await app.state.learning.list_nodes(path_id)
    registered = await MasteryQuizTool().run(
        ToolContext(
            turn_id="turn-1",
            session_id="sess-learn",
            language="zh",
            config={},
            metadata={},
            args={
                "node_id": nodes[node_index].id,
                "question": STEM,
                "expected_answer": "B",
                "question_type": "single",
                "options": OPTIONS,
                "explanation": "解析文字",
            },
        )
    )
    assert registered.ok is True, registered.output
    return {
        "path_id": path_id,
        "node": nodes[node_index],
        "question_id": registered.detail["question_id"],
    }


async def test_installs_only_when_something_to_wrap(hub):
    """没装暂停函数就什么都不做（同一份 metadata 还是原样）。"""
    _client, app = hub
    ctx = _ctx(Inner("B"))
    ctx.metadata.pop("ask_user_fn")
    install(ctx, StreamBus("turn-1"), app.state.learning)
    assert "ask_user_fn" not in ctx.metadata


async def test_card_face_is_rewritten_from_the_registered_question(hub):
    """模型在 ask_user 里换一道题：不作数，卡面按库里那道题重写。"""
    _client, app = hub
    seeded = await _register(app)
    inner = Inner("B")
    ctx = _ctx(inner)
    install(ctx, StreamBus("turn-1"), app.state.learning)

    reply = await ctx.metadata["ask_user_fn"](
        "【模型自己编的题面，不该出现在卡上】",
        [{"label": "A", "description": "瞎写的选项"}],
        "ask-1",
        allow_free_text=False,
        context="瞎写的小字",
    )
    assert reply == "B"
    assert inner.last["question"] == STEM
    assert [item["label"] for item in inner.last["options"]] == ["A", "B", "C"]
    assert [item["description"] for item in inner.last["options"]] == OPTIONS
    assert inner.last["context"] == f"节点《{seeded['node'].title}》"
    # 归位后一律放开手输（选择题也让用户直接敲标签）
    assert inner.last["allow_free_text"] is True


async def test_answer_is_recorded_before_the_model_sees_it(hub):
    """原话先落库：`mastery_grade` 读的就是这一列，模型转述改不动。"""
    _client, app = hub
    seeded = await _register(app)
    inner = Inner("选 B")
    ctx = _ctx(inner)
    install(ctx, StreamBus("turn-1"), app.state.learning)

    await ctx.metadata["ask_user_fn"](STEM, [], "ask-1")
    pending = await app.state.learning.pending_interaction(seeded["path_id"])
    assert pending is not None and pending.user_answer == "选 B"
    assert pending.status == "awaiting_input"  # 只填原话，判分还是 mastery_grade 的事
    # 同一条原话也压在 metadata 上（定性门取它当原话）
    assert ctx.metadata[ANSWER_KEY] == "选 B"


async def test_passes_through_when_no_path_is_bound(hub):
    """没绑路径：不是学习回合，一个字都不动。"""
    _client, app = hub
    await _register(app)
    inner = Inner("随便聊聊")
    ctx = _ctx(inner, session_id="sess-other")
    install(ctx, StreamBus("turn-1"), app.state.learning)

    reply = await ctx.metadata["ask_user_fn"]("普通澄清提问", [], "ask-1", allow_free_text=True)
    assert reply == "随便聊聊"
    assert inner.last["question"] == "普通澄清提问" and inner.last["allow_free_text"] is True
    assert ctx.metadata[ANSWER_KEY] == "随便聊聊"  # 原话照样记，只是没路径可落


async def test_passes_through_when_nothing_is_pending(hub):
    """路径绑着但没有未决题（模型只是问句话）：原样放行。"""
    _client, app = hub
    seeded = await _register(app)
    await app.state.learning.abandon_pending(seeded["path_id"])
    inner = Inner("B")
    ctx = _ctx(inner)
    install(ctx, StreamBus("turn-1"), app.state.learning)

    await ctx.metadata["ask_user_fn"]("想先讲哪一个？", [], "ask-1")
    assert inner.last["question"] == "想先讲哪一个？"


async def test_ignores_qualitative_card(hub):
    """定性门的卡没有登记题（question_id 空）：不归位，原样发。"""
    _client, app = hub
    seeded = await _register(app)
    learning = app.state.learning
    interaction = await learning.open_interaction(
        path_id=seeded["path_id"],
        node_id=seeded["node"].id,
        kind="assess",
        card_prompt=seeded["node"].title,
        session_id="sess-learn",
        turn_id="turn-1",
    )
    assert interaction.question_id == ""
    inner = Inner("讲一遍")
    ctx = _ctx(inner)
    install(ctx, StreamBus("turn-1"), app.state.learning)

    await ctx.metadata["ask_user_fn"]("请讲讲你对矩阵乘法的理解", [], "ask-1")
    assert inner.last["question"] == "请讲讲你对矩阵乘法的理解"


async def test_blank_reply_is_not_recorded(hub):
    """空作答（超时/跳过）不落库：未决行留着，下一回合优先重提。"""
    _client, app = hub
    seeded = await _register(app)
    inner = Inner("")
    ctx = _ctx(inner)
    install(ctx, StreamBus("turn-1"), app.state.learning)

    await ctx.metadata["ask_user_fn"](STEM, [], "ask-1")
    pending = await app.state.learning.pending_interaction(seeded["path_id"])
    assert pending is not None and pending.user_answer == ""


async def test_record_failure_does_not_break_the_turn(hub):
    """落库这步是 best-effort：炸了也把答复还给模型（不打断回合）。"""
    _client, app = hub
    await _register(app)
    inner = Inner("B")
    ctx = _ctx(inner)
    service = app.state.learning
    install(ctx, StreamBus("turn-1"), service)

    async def boom(*args, **kwargs):
        raise RuntimeError("库炸了")

    service.record_question_answer = boom  # type: ignore[method-assign]
    reply = await ctx.metadata["ask_user_fn"](STEM, [], "ask-1")
    assert reply == "B" and ctx.metadata[ANSWER_KEY] == "B"


async def test_path_lookup_error_falls_back_to_no_path(hub):
    """查路径炸了（比如库不可用）：按「没有路径」处理，别把卡扣住不发。"""
    _client, app = hub
    await _register(app)
    inner = Inner("B")
    ctx = _ctx(inner)
    service = app.state.learning
    install(ctx, StreamBus("turn-1"), service)

    async def boom(*args, **kwargs):
        raise RuntimeError("查不动")

    service.get_path_by_session = boom  # type: ignore[method-assign]
    await ctx.metadata["ask_user_fn"]("原样发出去", [], "ask-1")
    assert inner.last["question"] == "原样发出去"
