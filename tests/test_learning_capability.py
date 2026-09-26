"""mastery_path 能力的**状态块**：模型能看到的全部全局信息都在这一块里（零 LLM，纯服务端算）。

能力本身跑起来要真 LLM（金路径在 test_learning_pipeline），这里只单独喂它的状态块函数：
- 没绑路径 → 明说「先 paths 看已有的」，并把库里每条路径的标题/进度/下一目标列出来
  （模型看不到路径表，不给它这块就只能瞎 build）；
- 错题材料只含**已作答且答错过**的题（题干 + 正确答案 + 解析，重讲必需），
  未作答的题连题干都不出现、答案键更不出现——这是答案不外泄约束 ② 的唯一守门处；
- 有一张卡在飞时点名那道卡，下一目标也跟着变成「先把这张卡答完」。
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.capabilities.mastery.capability import MasteryCapability

NODES = [
    {"title": "向量与线性组合", "type": "concept"},
    {"title": "矩阵与初等变换", "type": "procedure", "parent_index": 0},
]
LEAF = [{"title": "矩阵与初等变换", "type": "procedure"}]

# 状态块是 (service, detail, lang) 的纯函数；ctx 只是 `_wrong_material` 的签名搭子
# （那里靠 get_question_bank() 读库，用不到 ctx，所以给个最小替身就够）
CTX = SimpleNamespace(language="zh")


@pytest.fixture
async def hub(tmp_home, repo_prompts):
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


async def _block(app, detail, lang: str = "zh") -> str:
    return await MasteryCapability()._state_block(CTX, app.state.learning, lang, detail)


async def test_state_block_lists_existing_paths_without_one(hub):
    _client, app = hub
    learning = app.state.learning

    empty = await _block(app, None)
    assert "本会话没有绑定的路径" in empty
    assert "switch" in empty and "build" in empty

    first, _ = await learning.create_path(topic="线性代数", nodes=NODES)
    second, _ = await learning.create_path(topic="概率论", nodes=LEAF)
    block = await _block(app, None)
    # 两条路径都给出来（id 也要给：switch 要拿它当参数）
    assert "线性代数" in block and first.id in block
    assert "概率论" in block and second.id in block
    # 给的是「下一目标 + 建议动作」，不是「当前节点」——那列已经删了
    assert "向量与线性组合" in block


async def test_wrong_material_only_feeds_answered_wrong_questions(hub):
    _client, app = hub
    learning, bank = app.state.learning, app.state.questions
    path, nodes = await learning.create_path(topic="线性代数", nodes=LEAF)
    node = nodes[0]
    answered = await bank.create_question(
        stem="答错的题面",
        options=[],
        type="short",
        answer="答案键·已作答",
        explanation="解析·已作答",
        node_id=node.id,
    )
    await bank.create_question(
        stem="没作答的题面",
        options=[],
        type="short",
        answer="答案键·未作答",
        explanation="解析·未作答",
        node_id=node.id,
    )
    await bank.record_attempt(
        answered.id, answer="写错了", correct=False, score=0.0, source="deterministic"
    )

    # 下一目标就是这道题所在的节点 → 错题材料跟的是「下一目标那个节点」
    detail = await learning.get_path(path.id)
    assert detail.next_target.node_id == node.id
    block = await _block(app, detail)

    # ① 已作答的错题：题干 + 正确答案 + 解析都进（不给这三样，模型没法针对性重讲）
    assert "答错的题面" in block
    assert "答案键·已作答" in block
    assert "解析·已作答" in block
    assert "这一轮的重点" in block  # remedial：让模型顺着错因讲
    # ② 未作答的题：题干都不给（只报数量），答案键更不许出现
    assert "没作答的题面" not in block
    assert "答案键·未作答" not in block
    assert "解析·未作答" not in block
    assert "2 道题（做过 1 道，错过 1 次）" in block


async def test_state_block_points_at_the_card_in_flight(hub):
    _client, app = hub
    learning, bank = app.state.learning, app.state.questions
    path, nodes = await learning.create_path(topic="线性代数", nodes=LEAF)
    question = await bank.create_question(
        stem="飞着的那道题",
        options=[],
        type="short",
        answer="答案键·未判分",
        explanation="解析·未判分",
        node_id=nodes[0].id,
    )
    await learning.open_interaction(
        path_id=path.id,
        node_id=nodes[0].id,
        question_id=question.id,
        kind="quiz",
        card_prompt=question.stem,
    )

    block = await _block(app, await learning.get_path(path.id))
    # 未决卡：题面可以进提示词（用户已经看到了），下一目标被它劫持
    assert "有一张卡还没作答" in block and "飞着的那道题" in block
    assert "先把这张卡答完" in block
    # 但这道题还没判分 → 答案键与解析一律不进提示词（约束 ② 对未决卡同样成立）
    assert "答案键·未判分" not in block
    assert "解析·未判分" not in block
