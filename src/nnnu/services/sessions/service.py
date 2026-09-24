"""会话管理（§6.8）：会话/消息 CRUD、标题、regenerate 支持。

- 同 session 单活动回合的互斥在传输层（TurnRuntime，纯内存），本层不负责；
- 标题：首条用户消息截断 50 字，仅当标题仍为空时设置；
- regenerate：删除末条 user 消息之后的全部 assistant 消息，末条 user 消息
  即 request_snapshot（含当回合引用与参数，重跑上下文一致）。
"""

import json
import time
from typing import Any

from nnnu.services.sessions.db import Database
from nnnu.services.sessions.models import Message, Session

TITLE_MAX_CHARS = 50  # §6.8 标题截断长度


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


class SessionManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- sessions ----

    async def ensure_session(
        self, session_id: str | None, *, capability: str = "chat", language: str = "zh"
    ) -> Session:
        """打开或创建：无 id → 新建；有 id 不存在 → 携带该 id 新建。"""
        if session_id is not None:
            existing = await self.get_session(session_id)
            if existing is not None:
                return existing
            session = Session(id=session_id, capability=capability, language=language)
        else:
            session = Session.new(capability=capability, language=language)
        await self._insert_session(session)
        return session

    async def get_session(self, session_id: str) -> Session | None:
        row = await self._db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._row_to_session(row) if row else None

    async def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[Session]:
        # rowid（插入序）做时间戳平局决胜：粗颗粒时钟（VM）下同 tick 写入时间戳相等
        rows = await self._db.fetch_all(
            "SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [self._row_to_session(row) for row in rows]

    async def rename_session(self, session_id: str, title: str) -> Session | None:
        await self._db.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, time.time(), session_id),
        )
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def set_persona(
        self, session_id: str, persona: str | None, persona_description: str | None
    ) -> Session | None:
        """设置会话 persona（§7.1 粘性）：persona 为预设 id / custom / None（清除）。"""
        await self._db.execute(
            "UPDATE sessions SET persona = ?, persona_description = ?, updated_at = ? WHERE id = ?",
            (persona, persona_description, time.time(), session_id),
        )
        return await self.get_session(session_id)

    async def set_kb_ids(self, session_id: str, kb_ids: list[str]) -> Session | None:
        """全量替换会话选中的知识库（§7.9 粘性；空列表 = 取消选择）。"""
        await self._db.execute(
            "UPDATE sessions SET kb_ids = ?, updated_at = ? WHERE id = ?",
            (_dumps(list(kb_ids)), time.time(), session_id),
        )
        return await self.get_session(session_id)

    async def set_model(self, session_id: str, model: str | None) -> Session | None:
        """设置会话模型覆盖（§6.10 粘性，'provider:model'；None = 清除，回退设置默认）。"""
        await self._db.execute(
            "UPDATE sessions SET model = ?, updated_at = ? WHERE id = ?",
            (model, time.time(), session_id),
        )
        return await self.get_session(session_id)

    async def set_capability(self, session_id: str, capability: str) -> Session | None:
        """设置会话能力（§6.4 粘性：回合显式带了 capability 就记下来，切走再回来还在）。"""
        await self._db.execute(
            "UPDATE sessions SET capability = ?, updated_at = ? WHERE id = ?",
            (capability, time.time(), session_id),
        )
        return await self.get_session(session_id)

    async def touch_session(self, session_id: str) -> None:
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )

    # ---- messages ----

    async def append_message(self, message: Message) -> None:
        await self._db.execute(
            """INSERT INTO messages
               (id, session_id, role, content, thinking, tool_calls, citations, cost, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.id,
                message.session_id,
                message.role,
                message.content,
                message.thinking,
                _dumps(message.tool_calls),
                _dumps(message.citations),
                _dumps(message.cost) if message.cost is not None else None,
                message.created_at,
            ),
        )

    async def list_messages(self, session_id: str) -> list[Message]:
        # rowid 决胜同 tick 时间戳（见 list_sessions 注释）
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        )
        return [self._row_to_message(row) for row in rows]

    async def get_last_message(self, session_id: str) -> Message | None:
        row = await self._db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        return self._row_to_message(row) if row else None

    async def get_last_user_message(self, session_id: str) -> Message | None:
        """末条 user 消息——regenerate 的 request_snapshot。"""
        row = await self._db.fetch_one(
            """SELECT * FROM messages WHERE session_id = ? AND role = 'user'
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (session_id,),
        )
        return self._row_to_message(row) if row else None

    async def delete_last_assistant(self, session_id: str) -> None:
        """删除末条 user 消息之后的所有 assistant 消息（regenerate 用）。

        用 rowid（插入序）而非 created_at 比较：同微秒写入时时间戳不可靠。
        """
        last_user = await self.get_last_user_message(session_id)
        if last_user is None:
            return
        await self._db.execute(
            """DELETE FROM messages WHERE session_id = ? AND role = 'assistant'
               AND rowid > (SELECT rowid FROM messages WHERE id = ?)""",
            (session_id, last_user.id),
        )

    async def set_title_from_first_user(self, session_id: str) -> None:
        """首条用户消息截断 50 字设为标题；仅当标题为空且会话存在时执行。"""
        session = await self.get_session(session_id)
        if session is None or session.title:
            return
        first_user = await self._db.fetch_one(
            """SELECT content FROM messages WHERE session_id = ? AND role = 'user'
               ORDER BY created_at ASC, rowid ASC LIMIT 1""",
            (session_id,),
        )
        if first_user is None:
            return
        title = first_user["content"].strip().replace("\n", " ")
        await self.rename_session(session_id, title[:TITLE_MAX_CHARS])

    # ---- 行映射 ----

    @staticmethod
    def _row_to_session(row: dict[str, Any]) -> Session:
        return Session(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            capability=row["capability"],
            model=row["model"],
            persona=row["persona"],
            persona_description=row["persona_description"],
            kb_ids=_loads(row["kb_ids"], []),
            tool_overrides=_loads(row["tool_overrides"], {}),
            language=row["language"] or "zh",
        )

    @staticmethod
    def _row_to_message(row: dict[str, Any]) -> Message:
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            thinking=row["thinking"],
            tool_calls=_loads(row["tool_calls"], []),
            citations=_loads(row["citations"], []),
            cost=_loads(row["cost"], None),
            created_at=row["created_at"],
        )

    async def _insert_session(self, session: Session) -> None:
        await self._db.execute(
            """INSERT INTO sessions
               (id, title, capability, model, persona, persona_description, language,
                kb_ids, tool_overrides, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                session.title,
                session.capability,
                session.model,
                session.persona,
                session.persona_description,
                session.language,
                _dumps(session.kb_ids),
                _dumps(session.tool_overrides),
                session.created_at,
                session.updated_at,
            ),
        )
