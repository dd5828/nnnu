"""aiosqlite 数据访问层：单连接 + WAL + 写事务串行（§4 技术栈）。

- 单连接（单用户场景写频率低，读不锁，写事务包 asyncio.Lock）；
- WAL + busy_timeout 兜底并发写；foreign_keys 每连接开启；
- 关闭必须显式 await close()——Windows 下泄漏连接会锁死 DB 文件。
"""

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import aiosqlite

T = TypeVar("T")


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._in_transaction = False

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database 未连接（先 await connect()）")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        conn = self._require_conn()
        cursor = await conn.execute(sql, params)
        if not self._in_transaction:  # 事务内由 transaction() 统一提交，早提交会破坏原子性
            await conn.commit()
        return cursor

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = self._require_conn()
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        conn = self._require_conn()
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def transaction(self, fn: Callable[[], Awaitable[T]]) -> T:
        """写事务：锁内 BEGIN IMMEDIATE → fn → COMMIT/ROLLBACK。fn 内勿再开事务。

        事务期间 execute 不自行提交（见 _in_transaction），读取走 fetch_* 即可。
        """
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            self._in_transaction = True
            try:
                result = await fn()
                await conn.commit()
                return result
            except BaseException:
                await conn.rollback()
                raise
            finally:
                self._in_transaction = False
