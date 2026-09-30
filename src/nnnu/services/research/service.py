"""调研仓储（§7.6）：`research_runs` 的增删查改 + 状态机 + 确认词表。

铁律与 learning 那边一致：**事务里绝不等 LLM**（`db.transaction` 是 BEGIN IMMEDIATE
单连接写锁），本模块全程纯 SQL——所有模型调用都在能力层，这里只负责把大纲、状态、
失败清单落库与读回。
"""

import json
import logging
import re
import sqlite3
import time

from nnnu.services.research.models import (
    ACTIVE_STATUSES,
    ACTIVE_TTL_S,
    RUN_STATUSES,
    ResearchRun,
    RunReport,
    RunSummary,
    SubTopic,
    parse_subtopics,
)
from nnnu.services.sessions.db import Database

logger = logging.getLogger(__name__)

# 「用户确认大纲」的自然语言兜底（前端按钮会带 config.research_action，
# 但直接把「确认」打进输入框也得走通）。刻意用词表而不是让模型判：
# 那会多吃一次 LLM 调用，把脚本化金路径的步数确定性毁掉。
# 长短语排在前面——剥离时按长度从长到短，才能让「确认，开始研究」整句剥空
CONFIRM_WORDS: tuple[str, ...] = (
    "开始研究",
    "确认研究",
    "确认开始",
    "就这样吧",
    "没问题",
    "开始吧",
    "开跑",
    "确认",
    "确定",
    "可以",
    "好的",
    "行",
    "好",
    "开始",
    "ok",
    "okay",
    "yes",
    "go",
    "proceed",
    "confirm",
    "looks good",
    "run it",
)
# 语气词/量词：剥掉它们不算「说了别的话」，「确认一下吧」跟「确认」是一回事
CONFIRM_FILLER: tuple[str, ...] = ("一下", "吧", "了", "的", "呀", "啊", "嗯", "呗")
_CONFIRM_PUNCT = re.compile(r"[，,。.!！?？~～；;：:、\s]+")


def is_confirmation(action: str, message: str) -> bool:
    """这句用户消息是不是「就按这个大纲来」。

    强信号优先（前端按钮下发 `config.research_action == "confirm"`）；
    否则要求**整句就是一句确认**：剥掉确认语与语气词后什么都不剩。

    刻意不做「包含确认词」的宽匹配——「可以把第二个子问题删掉吗」里也有「可以」，
    但那是修改请求，认成确认会让研究跑在一份用户并不认可的大纲上。反过来，
    长句里夹带了新要求（「确认，但重点看评测」）也该按修改处理，而不是当确认。
    """
    if action.strip().lower() in ("confirm", "confirmed", "yes"):
        return True
    text = _CONFIRM_PUNCT.sub(" ", message).strip().lower()
    if not text:
        return False
    hit = False
    for word in sorted(CONFIRM_WORDS, key=len, reverse=True):
        if word in text:
            hit = True
            text = text.replace(word, " ")
    for filler in CONFIRM_FILLER:
        text = text.replace(filler, " ")
    return hit and not text.strip()


class ResearchError(ValueError):
    """仓储层错误（非法状态流转等），由能力层转成用户可读文案。"""


class ResearchService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 读 ----

    async def get_run(self, run_id: str) -> ResearchRun | None:
        row = await self._db.fetch_one("SELECT * FROM research_runs WHERE id = ?", (run_id,))
        return _to_run(row) if row is not None else None

    async def active_run(self, session_id: str, *, now: float | None = None) -> ResearchRun | None:
        """这条会话在飞的调研（没有 / 只剩过期的一律 None）。

        TTL 是必要的：一张三天前没人理的大纲卡不该把这条会话永远钉在 confirming 上——
        过期直接当没有，用户重新提问就是一份新调研。
        """
        now = time.time() if now is None else now
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        row = await self._db.fetch_one(
            f"SELECT * FROM research_runs WHERE session_id = ? AND status IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, *ACTIVE_STATUSES),
        )
        if row is None:
            return None
        run = _to_run(row)
        if now - run.updated_at > ACTIVE_TTL_S:
            logger.info("调研 %s 超过 TTL，视为已放弃", run.id)
            await self._set_status(run.id, "abandoned", now=now)
            return None
        return run

    async def latest_run(
        self, session_id: str, statuses: tuple[str, ...] = ("reported", "partial")
    ) -> ResearchRun | None:
        """最近一条终态调研（`regenerate` 复用同一条用户消息时靠它找回大纲）。"""
        if not statuses:
            return None
        placeholders = ", ".join("?" for _ in statuses)
        row = await self._db.fetch_one(
            f"SELECT * FROM research_runs WHERE session_id = ? AND status IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, *statuses),
        )
        return _to_run(row) if row is not None else None

    async def list_runs(self, *, limit: int = 50, offset: int = 0) -> list[RunSummary]:
        """历次调研，新的在前（「我的历次调研」全局视图，不分会话）。

        会话标题 LEFT JOIN 进来：会话删了标题退化成空串，行本身照旧列出来。
        全局排序走不到 `(session_id, created_at)` 那条索引、要全表扫——单机单人量级
        （一次调研一行）可以接受；真到了要分页十万行的规模再补 created_at 索引。
        """
        rows = await self._db.fetch_all(
            "SELECT r.*, s.title AS session_title FROM research_runs r "
            "LEFT JOIN sessions s ON s.id = r.session_id "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [
            RunSummary(run=_to_run(row), session_title=row.get("session_title") or "")
            for row in rows
        ]

    async def count_runs(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM research_runs")
        return int(row["n"]) if row is not None else 0

    async def report_of(self, run: ResearchRun) -> RunReport | None:
        """成稿那条助手消息：确认消息之后的第一条 assistant（按 rowid，不靠时间戳）。

        `answer_message_id` 为空（还没确认就放弃了）或确认消息已被删 → 直接 None
        （子查询落空时 `rowid > NULL` 恒为 NULL，一条都选不出来）。
        """
        if not run.answer_message_id:
            return None
        row = await self._db.fetch_one(
            "SELECT * FROM messages WHERE session_id = ? AND role = 'assistant' "
            "AND rowid > (SELECT rowid FROM messages WHERE id = ?) "
            "ORDER BY rowid ASC LIMIT 1",
            (run.session_id, run.answer_message_id),
        )
        if row is None:
            return None
        raw_citations = row.get("citations")
        citations: list[dict] = []
        if isinstance(raw_citations, str) and raw_citations:
            try:
                parsed = json.loads(raw_citations)
            except (TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                citations = [item for item in parsed if isinstance(item, dict)]
        return RunReport(
            message_id=row["id"],
            content_md=row.get("content") or "",
            citations=citations,
            created_at=float(row.get("created_at") or 0.0),
        )

    # ---- 写 ----

    async def create_run(
        self,
        *,
        session_id: str,
        topic: str,
        refined_topic: str,
        mode: str,
        depth: str,
        subtopics: list[SubTopic],
        now: float | None = None,
    ) -> ResearchRun:
        """建一条 confirming 的行；撞上「同会话已有在飞」就作废旧行重试一次。

        照 `open_interaction` 的做法（`learning/service.py`）：旧行多半是上次崩溃留下的，
        作废后重试；仍失败让异常往上抛——不当场静默吞掉。
        """
        now = time.time() if now is None else now
        run = ResearchRun.new(
            session_id=session_id,
            topic=topic,
            refined_topic=refined_topic,
            mode=mode,
            depth=depth,
            subtopics=subtopics,
            status="confirming",
            now=now,
        )

        async def _write() -> None:
            await self._abandon_active_now(session_id, now)
            await self._db.execute(
                "INSERT INTO research_runs "
                "(id, session_id, topic, refined_topic, mode, depth, subtopics, status, "
                " failed_subtopics, answer_message_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'confirming', '[]', NULL, ?, ?)",
                (
                    run.id,
                    run.session_id,
                    run.topic,
                    run.refined_topic,
                    run.mode,
                    run.depth,
                    _dump_subtopics(subtopics),
                    run.created_at,
                    run.updated_at,
                ),
            )

        try:
            await self._db.transaction(_write)
        except sqlite3.IntegrityError:
            logger.warning("在飞调研索引冲突，作废旧行后重试一次")
            await self._db.transaction(_write)
        return run

    async def confirm_run(
        self,
        run_id: str,
        *,
        subtopics: list[SubTopic],
        answer_message_id: str | None,
        now: float | None = None,
    ) -> None:
        """用户确认大纲：confirming → researching，顺手把（可能被改过的）大纲定稿。"""
        now = time.time() if now is None else now
        await self._db.execute(
            "UPDATE research_runs SET status = 'researching', subtopics = ?, "
            "answer_message_id = ?, updated_at = ? WHERE id = ?",
            (_dump_subtopics(subtopics), answer_message_id, now, run_id),
        )

    async def update_subtopics(
        self,
        run_id: str,
        *,
        subtopics: list[SubTopic],
        refined_topic: str | None = None,
        now: float | None = None,
    ) -> None:
        """用户在确认阶段提了修改：就地换掉大纲（不新建行，免得丢会话绑定）。"""
        now = time.time() if now is None else now
        if refined_topic is None:
            await self._db.execute(
                "UPDATE research_runs SET subtopics = ?, updated_at = ? WHERE id = ?",
                (_dump_subtopics(subtopics), now, run_id),
            )
        else:
            await self._db.execute(
                "UPDATE research_runs SET subtopics = ?, refined_topic = ?, updated_at = ? "
                "WHERE id = ?",
                (_dump_subtopics(subtopics), refined_topic, now, run_id),
            )

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        failed_subtopics: list[str] | None = None,
        now: float | None = None,
    ) -> None:
        """收尾：reported（全部跑完）或 partial（有子问题没跑完）。

        不记报告那条消息的 id：它在能力返回之后才生成（见 schema v10 的注释）。
        """
        if status not in ("reported", "partial"):
            raise ResearchError(f"收尾状态只能是 reported/partial，收到 {status!r}")
        now = time.time() if now is None else now
        await self._db.execute(
            "UPDATE research_runs SET status = ?, failed_subtopics = ?, updated_at = ? WHERE id = ?",
            (
                status,
                json.dumps(failed_subtopics or [], ensure_ascii=False),
                now,
                run_id,
            ),
        )

    async def abandon_run(self, run_id: str, *, now: float | None = None) -> None:
        """丢弃（stop / 能力异常）：终态不占部分唯一索引，下次提问就是新调研。"""
        await self._set_status(run_id, "abandoned", now=now)

    # ---- 内部 ----

    async def _set_status(self, run_id: str, status: str, *, now: float | None = None) -> None:
        if status not in RUN_STATUSES:
            raise ResearchError(f"未知调研状态 {status!r}")
        now = time.time() if now is None else now
        await self._db.execute(
            "UPDATE research_runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, run_id),
        )

    async def _abandon_active_now(self, session_id: str, now: float) -> None:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        await self._db.execute(
            f"UPDATE research_runs SET status = 'abandoned', updated_at = ? "
            f"WHERE session_id = ? AND status IN ({placeholders})",
            (now, session_id, *ACTIVE_STATUSES),
        )


def _dump_subtopics(subtopics: list[SubTopic]) -> str:
    return json.dumps(
        [{"title": item.title, "overview": item.overview} for item in subtopics],
        ensure_ascii=False,
    )


def _to_run(row: dict) -> ResearchRun:
    raw_failed = row.get("failed_subtopics") or "[]"
    try:
        failed = json.loads(raw_failed)
    except (TypeError, ValueError):
        failed = []
    return ResearchRun(
        id=row["id"],
        session_id=row["session_id"],
        topic=row.get("topic") or "",
        refined_topic=row.get("refined_topic") or "",
        mode=row.get("mode") or "report",
        depth=row.get("depth") or "standard",
        subtopics=parse_subtopics(json.loads(row.get("subtopics") or "[]")),
        status=row.get("status") or "confirming",
        failed_subtopics=[str(item) for item in failed] if isinstance(failed, list) else [],
        answer_message_id=row.get("answer_message_id"),
        created_at=float(row.get("created_at") or 0.0),
        updated_at=float(row.get("updated_at") or 0.0),
    )


_service: ResearchService | None = None


def set_research_service(service: ResearchService | None) -> None:
    """单例（照 learning/question_bank 的做法）：能力层要取，路由/app.state 也要取。"""
    global _service
    _service = service


def get_research_service() -> ResearchService:
    if _service is None:
        raise RuntimeError("ResearchService 未装配（见 api/main.py lifespan）")
    return _service
