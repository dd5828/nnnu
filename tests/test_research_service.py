"""调研仓储（§7.6）：草稿本 CRUD、状态机、部分唯一索引、确认词表——纯服务层，无 LLM。

用带 app 句柄的 client 起 lifespan（服务装配与真实运行一致，顺便钉住 `api/main.py`
确实把 `ResearchService` 装上了）；断言直接读库/调服务。
"""

import time

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.research.models import ACTIVE_TTL_S, DEPTH_SPECS, ResearchRun, SubTopic
from nnnu.services.research.service import (
    CONFIRM_WORDS,
    ResearchError,
    ResearchService,
    is_confirmation,
)

NOW = time.time()

OUTLINE = [
    SubTopic(title="什么是 RAG", overview="检索增强生成的基本链路"),
    SubTopic(title="主流方案", overview="向量库 / 混合检索 / 重排"),
]


@pytest.fixture
async def service(tmp_home) -> ResearchService:
    """(service)：走完整 lifespan，拿到的是能力层到时候会取到的那个单例。"""
    from nnnu.api.main import create_app
    from nnnu.services.research.service import get_research_service

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert c is not None
            yield get_research_service()


async def _create(service: ResearchService, session_id="sess-a", **overrides):
    kwargs = dict(
        session_id=session_id,
        topic="2025 年 RAG 的主流方案",
        refined_topic="调研 2025 年 RAG 的主流方案与取舍",
        mode="report",
        depth="standard",
        subtopics=OUTLINE,
        now=NOW,
    )
    kwargs.update(overrides)
    return await service.create_run(**kwargs)


# ---- 建行与读回 ----


async def test_create_and_read_back(service):
    run = await _create(service)
    assert run.id.startswith("rrun-")  # 前缀在 KNOWN_PREFIXES 里，拼错会直接抛
    assert run.status == "confirming"
    assert run.is_active

    again = await service.get_run(run.id)
    assert again is not None
    # 大纲是一次往返之间唯一的信物，往返后必须一字不差
    assert [item.title for item in again.subtopics] == ["什么是 RAG", "主流方案"]
    assert again.subtopics[0].overview == "检索增强生成的基本链路"
    assert again.spec is DEPTH_SPECS["standard"]


async def test_get_run_missing_returns_none(service):
    assert await service.get_run("rrun-nope") is None


async def test_topic_is_truncated_on_create(service):
    run = await _create(service, topic="长" * 900)
    assert len(run.topic) == 500  # MAX_TOPIC_CHARS，防止整段粘贴把列撑爆


async def test_active_run_is_per_session(service):
    run = await _create(service, session_id="sess-a")
    assert (await service.active_run("sess-a", now=NOW)).id == run.id
    assert await service.active_run("sess-b", now=NOW) is None


# ---- 「一路会话只有一个在飞」的部分唯一索引 ----


async def test_second_create_abandons_the_first(service):
    first = await _create(service)
    second = await _create(service, topic="另一个题目")
    assert (await service.get_run(first.id)).status == "abandoned"
    assert (await service.active_run("sess-a", now=NOW)).id == second.id


async def test_stale_active_row_is_replaced_not_blocked(service):
    """别的进程留下的在飞行（模拟崩在确认前）：新建时作废掉，不撞索引。"""
    stale = await _create(service)
    await service.confirm_run(stale.id, subtopics=OUTLINE, answer_message_id=None, now=NOW - 99999)
    fresh = await _create(service, topic="崩溃后重来")
    assert (await service.get_run(stale.id)).status == "abandoned"
    assert (await service.active_run("sess-a", now=NOW)).id == fresh.id


async def test_ttl_expiry_frees_the_session(service):
    """三天前没人理的大纲卡不该把会话永远钉在 confirming 上。"""
    old = await _create(service)
    assert await service.active_run("sess-a", now=NOW + ACTIVE_TTL_S + 1) is None
    assert (await service.get_run(old.id)).status == "abandoned"  # 顺手收进终态
    # 收走之后同会话立刻能开新调研（终态不占部分唯一索引）
    fresh = await _create(service)
    assert (await service.active_run("sess-a", now=NOW)).id == fresh.id


# ---- 状态流转 ----


async def test_confirm_moves_to_researching(service):
    run = await _create(service)
    edited = [*OUTLINE, SubTopic(title="成本与延迟", overview="")]
    await service.confirm_run(run.id, subtopics=edited, answer_message_id="msg-1", now=NOW + 1)
    cur = await service.get_run(run.id)
    assert cur.status == "researching"
    assert cur.answer_message_id == "msg-1"
    assert len(cur.subtopics) == 3  # 用户改过的大纲以确认那一刻的为准
    assert cur.is_active


async def test_update_subtopics_keeps_run_id(service):
    """修改大纲就地改这一行：新建行会把 answer_message_id 与会话绑定丢掉。"""
    run = await _create(service)
    await service.update_subtopics(
        run.id, subtopics=[SubTopic(title="只剩一个")], refined_topic="收窄后的题目", now=NOW + 1
    )
    cur = await service.get_run(run.id)
    assert [item.title for item in cur.subtopics] == ["只剩一个"]
    assert cur.refined_topic == "收窄后的题目"
    assert cur.status == "confirming"  # 还没确认，仍在等用户


async def test_finish_run_records_failures(service):
    run = await _create(service)
    await service.confirm_run(run.id, subtopics=OUTLINE, answer_message_id="msg-1", now=NOW)
    await service.finish_run(
        run.id,
        status="partial",
        failed_subtopics=["主流方案"],
        now=NOW + 5,
    )
    cur = await service.get_run(run.id)
    assert cur.status == "partial"
    assert cur.failed_subtopics == ["主流方案"]
    assert not cur.is_active  # 终态：不再占住这条会话


async def test_finish_run_rejects_unknown_status(service):
    run = await _create(service)
    with pytest.raises(ResearchError, match="reported/partial"):
        await service.finish_run(run.id, status="researching")


async def test_abandon_is_a_terminal_state(service):
    run = await _create(service)
    await service.abandon_run(run.id, now=NOW + 3)
    assert (await service.get_run(run.id)).status == "abandoned"
    assert await service.active_run("sess-a", now=NOW + 4) is None


async def test_latest_run_finds_finished_run(service):
    """regenerate 复用同一条用户消息时，靠它找回上一份大纲。"""
    first = await _create(service)
    await service.finish_run(first.id, status="reported", now=NOW + 1)
    assert (await service.latest_run("sess-a")).id == first.id
    # 新建一条在飞的：latest_run 只认终态，别把等确认的那条当成上一份
    second = await _create(service, now=NOW + 10)
    assert (await service.latest_run("sess-a")).id == first.id
    assert (await service.active_run("sess-a", now=NOW + 10)).id == second.id


async def test_latest_run_defaults_exclude_abandoned(service):
    run = await _create(service)
    await service.abandon_run(run.id, now=NOW)
    assert await service.latest_run("sess-a") is None


async def test_unknown_depth_falls_back_to_standard(service):
    """入库值理论上一定在 DEPTHS 里，但手改库/旧库不该让收尾时抛 KeyError。"""
    run = await _create(service, depth="bogus")
    assert run.spec.subtopics == DEPTH_SPECS["standard"].subtopics


# ---- 确认词表 ----


@pytest.mark.parametrize(
    "message",
    [
        "确认",
        "确认一下",
        " 确认 ",
        "确认！",
        "确认一下吧",
        "好的",
        "开始吧",
        "可以",
        "没问题",
        "ok",
        "OK，开始",
        "确认，开始研究",  # 前端确认按钮发出去的原话
        "looks good",
        "run it",
    ],
)
def test_short_affirmatives_are_confirmations(message):
    assert is_confirmation("", message)


@pytest.mark.parametrize(
    "message",
    [
        "确认一下第三点是不是应该换成成本",
        "可以把第二个子问题删掉吗",  # 长句里的「可以」是提问，不是确认
        "确认，但重点看评测部分",  # 确认里夹带了新要求 → 按修改处理
        "第三个换成「评测基准」",
        "再补充一个子问题",
        "确定要研究这个方向吗",
        "吧",  # 只有语气词，什么都没确认
        "",
        "   ",
    ],
)
def test_long_or_unmatched_messages_are_not_confirmations(message):
    assert not is_confirmation("", message)


def test_button_action_overrides_message_text():
    """前端按钮是强信号：正文写什么都不用猜。"""
    assert is_confirmation("confirm", "就按这个大纲查")


@pytest.mark.parametrize("word", CONFIRM_WORDS)
def test_word_list_is_self_consistent(word):
    """表里每一个词单独说出来都得算确认——否则它就是个永远命不中的死条目。"""
    assert is_confirmation("", word)


# ---- 模型层 ----


def test_new_run_defaults_mode_and_depth():
    run = ResearchRun.new(
        session_id="sess-x",
        topic="题目",
        refined_topic="",
        mode="report",
        depth="standard",
        subtopics=OUTLINE,
        now=NOW,
    )
    assert (run.status, run.failed_subtopics, run.created_at) == ("confirming", [], NOW)
    assert run.answer_message_id is None
