"""会话管理：CRUD、标题截断、regenerate 支持、级联删除。"""

import pytest

from nnnu.services.sessions.db import Database
from nnnu.services.sessions.models import Message, Session
from nnnu.services.sessions.schema import db_path, migrate
from nnnu.services.sessions.service import SessionManager


@pytest.fixture
async def manager(tmp_home):
    """迁移 + 连接后的 SessionManager（每测试独立 tmp 数据目录）。"""
    data_root = tmp_home / "data"
    migrate(data_root)
    db = Database(db_path(data_root))
    await db.connect()
    try:
        yield SessionManager(db)
    finally:
        await db.close()


async def test_ensure_session_create_and_reuse(manager):
    created = await manager.ensure_session(None)
    assert created.id.startswith("sess-")
    assert created.title == ""
    # 同 id 复用
    reused = await manager.ensure_session(created.id)
    assert reused.id == created.id
    # 指定 id 不存在时携带该 id 新建
    custom = await manager.ensure_session("sess-fixed01")
    assert custom.id == "sess-fixed01"


async def test_ensure_session_new_id_each_time(manager):
    a = await manager.ensure_session(None)
    b = await manager.ensure_session(None)
    assert a.id != b.id


async def test_title_truncate_50(manager):
    session = await manager.ensure_session(None)
    long_text = "长" * 80
    await manager.append_message(Message.new(session_id=session.id, role="user", content=long_text))
    await manager.set_title_from_first_user(session.id)
    loaded = await manager.get_session(session.id)
    assert loaded.title == "长" * 50


async def test_title_not_overwritten(manager):
    session = await manager.ensure_session(None)
    await manager.rename_session(session.id, "自定义标题")
    await manager.append_message(Message.new(session_id=session.id, role="user", content="新消息"))
    await manager.set_title_from_first_user(session.id)
    loaded = await manager.get_session(session.id)
    assert loaded.title == "自定义标题"


async def test_list_messages_order(manager):
    session = await manager.ensure_session(None)
    m1 = Message.new(session_id=session.id, role="user", content="1", created_at=1.0)
    m2 = Message.new(session_id=session.id, role="assistant", content="2", created_at=2.0)
    # 逆序插入，验证按 created_at 升序返回（与插入顺序无关）
    await manager.append_message(m2)
    await manager.append_message(m1)
    messages = await manager.list_messages(session.id)
    assert [m.content for m in messages] == ["1", "2"]


async def test_message_roundtrip_full_fields(manager):
    session = await manager.ensure_session(None)
    message = Message.new(
        session_id=session.id,
        role="assistant",
        content="答案",
        thinking="思考",
        tool_calls=[{"name": "add", "args": {"a": 1}}],
        citations=[{"doc_id": "d1"}],
        cost={"tokens": 10, "cost": 0.001},
    )
    await manager.append_message(message)
    loaded = await manager.list_messages(session.id)
    assert loaded[0].thinking == "思考"
    assert loaded[0].tool_calls == [{"name": "add", "args": {"a": 1}}]
    assert loaded[0].citations == [{"doc_id": "d1"}]
    assert loaded[0].cost == {"tokens": 10, "cost": 0.001}


async def test_delete_last_assistant(manager):
    session = await manager.ensure_session(None)
    u1 = Message.new(session_id=session.id, role="user", content="q1", created_at=1.0)
    a1 = Message.new(session_id=session.id, role="assistant", content="a1", created_at=2.0)
    u2 = Message.new(session_id=session.id, role="user", content="q2", created_at=3.0)
    a2 = Message.new(session_id=session.id, role="assistant", content="a2", created_at=4.0)
    for m in (u1, a1, u2, a2):
        await manager.append_message(m)
    await manager.delete_last_assistant(session.id)
    messages = await manager.list_messages(session.id)
    assert [m.content for m in messages] == ["q1", "a1", "q2"]


async def test_delete_last_assistant_when_no_reply(manager):
    session = await manager.ensure_session(None)
    await manager.append_message(Message.new(session_id=session.id, role="user", content="q1"))
    await manager.delete_last_assistant(session.id)  # 无 assistant 不报错
    messages = await manager.list_messages(session.id)
    assert len(messages) == 1


async def test_get_last_user_message(manager):
    session = await manager.ensure_session(None)
    await manager.append_message(Message.new(session_id=session.id, role="user", content="q1"))
    await manager.append_message(Message.new(session_id=session.id, role="assistant", content="a1"))
    last_user = await manager.get_last_user_message(session.id)
    assert last_user.content == "q1"


async def test_delete_session_cascades_messages(manager):
    session = await manager.ensure_session(None)
    await manager.append_message(Message.new(session_id=session.id, role="user", content="q"))
    await manager.delete_session(session.id)
    assert await manager.get_session(session.id) is None
    assert await manager.list_messages(session.id) == []


async def test_list_sessions_ordered_by_updated_at(manager):
    await manager.ensure_session(None)
    s2 = await manager.ensure_session(None)
    await manager.touch_session(s2.id)
    sessions = await manager.list_sessions()
    assert sessions[0].id == s2.id


async def test_session_json_fields_roundtrip(manager):
    session = Session.new(kb_ids=["kb-1"], tool_overrides={"web_search": True}, model="m1")
    await manager._insert_session(session)
    loaded = await manager.get_session(session.id)
    assert loaded.kb_ids == ["kb-1"]
    assert loaded.tool_overrides == {"web_search": True}
    assert loaded.model == "m1"
