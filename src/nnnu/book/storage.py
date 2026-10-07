"""Book 存储：books / book_pages / book_page_messages / book_attempts 的 DB 读写（schema v14）。

本模块只做行 ↔ 模型转换与 CRUD，不含业务规则（校验在 models.py，编排在 service.py）。
规则：
- 一本书一把 asyncio.Lock（懒建）：编译任务与写端点共用它做单书互斥，不同书无关；
- 页的 blocks 是 JSON 数组列——整页替换式写入（块级落库也走同一条，调用方自己拼数组）；
- 删章 = 删页：聊天消息与作答靠 ON DELETE CASCADE 一起走（连接已开 foreign_keys=ON）。
"""

import asyncio
import json
import time
from typing import Any

from nnnu.book.models import Attempt, Block, Book, BookPage, PageMessage, Spine
from nnnu.services.sessions.db import Database


def _loads(raw: Any, fallback: Any) -> Any:
    """JSON 列解析：行都是我们自己写的；真坏了按 fallback 读（读取端不炸）。"""
    if not isinstance(raw, str) or not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


class BookStorage:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._locks: dict[str, asyncio.Lock] = {}

    def book_lock(self, book_id: str) -> asyncio.Lock:
        """每书一把（懒建，进程内）：同书编译/编辑互斥；不同书互不影响。"""
        lock = self._locks.get(book_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[book_id] = lock
        return lock

    # ---- books ----

    async def list_books(self) -> list[Book]:
        rows = await self._db.fetch_all("SELECT * FROM books ORDER BY updated_at DESC, rowid DESC")
        return [self._row_to_book(row) for row in rows]

    async def get_book(self, book_id: str) -> Book | None:
        row = await self._db.fetch_one("SELECT * FROM books WHERE id = ?", (book_id,))
        return self._row_to_book(row) if row else None

    async def insert_book(self, book: Book) -> Book:
        await self._db.execute(
            "INSERT INTO books (id, title, sources, spine, status, fingerprints, error, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book.id,
                book.title,
                json.dumps(book.sources, ensure_ascii=False),
                json.dumps(book.spine, ensure_ascii=False),
                book.status,
                json.dumps(book.fingerprints, ensure_ascii=False),
                book.error,
                book.created_at,
                book.updated_at,
            ),
        )
        return book

    async def update_book(
        self,
        book_id: str,
        *,
        title: str | None = None,
        spine: dict[str, Any] | Spine | None = None,
        status: str | None = None,
        fingerprints: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Book | None:
        """None = 不改；spine/fingerprints 整体替换；updated_at 总刷新。"""
        updates: list[str] = []
        params: list[Any] = []
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if spine is not None:
            payload = spine.model_dump() if isinstance(spine, Spine) else spine
            updates.append("spine = ?")
            params.append(json.dumps(payload, ensure_ascii=False))
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if fingerprints is not None:
            updates.append("fingerprints = ?")
            params.append(json.dumps(fingerprints, ensure_ascii=False))
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if not updates:
            current = await self.get_book(book_id)
            return current
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(book_id)
        cursor = await self._db.execute(
            f"UPDATE books SET {', '.join(updates)} WHERE id = ?", tuple(params)
        )
        if cursor.rowcount == 0:
            return None
        return await self.get_book(book_id)

    async def delete_book(self, book_id: str) -> bool:
        """删书：页/消息/作答按外键级联一起走。"""
        cursor = await self._db.execute("DELETE FROM books WHERE id = ?", (book_id,))
        return cursor.rowcount > 0

    def _row_to_book(self, row: dict[str, Any]) -> Book:
        return Book(
            id=row["id"],
            title=row["title"],
            sources=_loads(row.get("sources"), {}),
            spine=_loads(row.get("spine"), {}),
            status=row.get("status") or "draft",
            fingerprints=_loads(row.get("fingerprints"), {}),
            error=row.get("error") or "",
            created_at=row.get("created_at") or 0.0,
            updated_at=row.get("updated_at") or 0.0,
        )

    # ---- pages ----

    async def list_pages(self, book_id: str) -> list[BookPage]:
        rows = await self._db.fetch_all(
            "SELECT * FROM book_pages WHERE book_id = ? ORDER BY page_no, rowid", (book_id,)
        )
        return [self._row_to_page(row) for row in rows]

    async def get_page(self, book_id: str, page_id: str) -> BookPage | None:
        """按书域取页：跨书拿别的书的页一律当不存在（路由就不必再查 book_id）。"""
        row = await self._db.fetch_one(
            "SELECT * FROM book_pages WHERE id = ? AND book_id = ?", (page_id, book_id)
        )
        return self._row_to_page(row) if row else None

    async def sync_pages(self, book_id: str, chapters: list[dict[str, Any]]) -> list[BookPage]:
        """把页与 spine 章节对齐（改名保页、删章删页、按新顺序重排 page_no）。

        draft 阶段改一章名不该清掉已编译的块——所以 upsert 按 chapter_key，只动序号；
        chapter_key 不在新 spine 里的页整行删除（聊天/作答级联走）。
        """
        existing = {page.chapter_key: page for page in await self.list_pages(book_id)}
        keep_keys = {str(chapter.get("key")) for chapter in chapters}
        for chapter_key, existing_page in existing.items():
            if chapter_key not in keep_keys:
                await self._db.execute("DELETE FROM book_pages WHERE id = ?", (existing_page.id,))
        pages: list[BookPage] = []
        for index, chapter in enumerate(chapters, start=1):
            key = str(chapter.get("key"))
            page = existing.get(key)
            if page is None:
                page = BookPage.new(book_id=book_id, chapter_key=key, page_no=index)
                await self._db.execute(
                    "INSERT INTO book_pages (id, book_id, chapter_key, page_no, blocks, "
                    "visited, bookmarked, updated_at) VALUES (?, ?, ?, ?, '[]', 0, 0, ?)",
                    (page.id, page.book_id, page.chapter_key, page.page_no, page.updated_at),
                )
            elif page.page_no != index:
                await self._db.execute(
                    "UPDATE book_pages SET page_no = ?, updated_at = ? WHERE id = ?",
                    (index, time.time(), page.id),
                )
                page.page_no = index
            pages.append(page)
        return pages

    async def get_page_by_chapter(self, book_id: str, chapter_key: str) -> BookPage | None:
        row = await self._db.fetch_one(
            "SELECT * FROM book_pages WHERE book_id = ? AND chapter_key = ?",
            (book_id, chapter_key),
        )
        return self._row_to_page(row) if row else None

    async def update_page_blocks(self, page_id: str, blocks: list[Block]) -> None:
        """整页块数组替换式写入（块级增量也拼整数组后再进来——数组不大，简单优先）。"""
        payload = json.dumps([block.model_dump() for block in blocks], ensure_ascii=False)
        await self._db.execute(
            "UPDATE book_pages SET blocks = ?, updated_at = ? WHERE id = ?",
            (payload, time.time(), page_id),
        )

    async def update_page_flags(
        self,
        page_id: str,
        *,
        visited: bool | None = None,
        bookmarked: bool | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[Any] = []
        if visited is not None:
            updates.append("visited = ?")
            params.append(1 if visited else 0)
        if bookmarked is not None:
            updates.append("bookmarked = ?")
            params.append(1 if bookmarked else 0)
        if not updates:
            return
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(page_id)
        await self._db.execute(
            f"UPDATE book_pages SET {', '.join(updates)} WHERE id = ?", tuple(params)
        )

    def _row_to_page(self, row: dict[str, Any]) -> BookPage:
        raw_blocks = _loads(row.get("blocks"), [])
        blocks: list[Block] = []
        for item in raw_blocks:
            if not isinstance(item, dict):
                continue
            try:
                blocks.append(Block.model_validate(item))
            except Exception:  # 单块坏了不连坐整页（读取端宽进）
                continue
        return BookPage(
            id=row["id"],
            book_id=row["book_id"],
            chapter_key=row["chapter_key"],
            page_no=row["page_no"],
            blocks=blocks,
            visited=bool(row.get("visited")),
            bookmarked=bool(row.get("bookmarked")),
            updated_at=row.get("updated_at") or 0.0,
        )

    # ---- 页聊天消息 ----

    async def add_message(self, message: PageMessage) -> PageMessage:
        await self._db.execute(
            "INSERT INTO book_page_messages (id, page_id, role, content_md, citations, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.page_id,
                message.role,
                message.content_md,
                json.dumps(message.citations, ensure_ascii=False),
                message.created_at,
            ),
        )
        return message

    async def list_messages(self, page_id: str, *, limit: int | None = None) -> list[PageMessage]:
        """按时间正序；limit 时取最近 N 条（再翻回正序——页聊天历史和会话消息同口径）。"""
        if limit is None:
            rows = await self._db.fetch_all(
                "SELECT * FROM book_page_messages WHERE page_id = ? ORDER BY created_at, rowid",
                (page_id,),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM book_page_messages WHERE page_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (page_id, limit),
            )
            rows.reverse()
        return [self._row_to_message(row) for row in rows]

    def _row_to_message(self, row: dict[str, Any]) -> PageMessage:
        return PageMessage(
            id=row["id"],
            page_id=row["page_id"],
            role=row["role"],
            content_md=row.get("content_md") or "",
            citations=_loads(row.get("citations"), []),
            created_at=row.get("created_at") or 0.0,
        )

    # ---- 测验作答 ----

    async def add_attempt(self, attempt: Attempt) -> Attempt:
        await self._db.execute(
            "INSERT INTO book_attempts (id, book_id, page_id, block_id, answer, correct, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.id,
                attempt.book_id,
                attempt.page_id,
                attempt.block_id,
                json.dumps(attempt.answer, ensure_ascii=False),
                1 if attempt.correct else 0,
                attempt.created_at,
            ),
        )
        return attempt

    async def list_attempts(self, book_id: str) -> list[Attempt]:
        rows = await self._db.fetch_all(
            "SELECT * FROM book_attempts WHERE book_id = ? ORDER BY created_at, rowid",
            (book_id,),
        )
        return [self._row_to_attempt(row) for row in rows]

    async def delete_block_attempts(self, page_id: str, block_id: str) -> None:
        """删块/重生成块时清该块作答（block_id 在 JSON 里，外键管不到）。"""
        await self._db.execute(
            "DELETE FROM book_attempts WHERE page_id = ? AND block_id = ?", (page_id, block_id)
        )

    async def delete_page_attempts(self, page_id: str) -> None:
        """整章重编译时清该页作答：块 id 会整批换新，旧作答对不上任何块了。"""
        await self._db.execute("DELETE FROM book_attempts WHERE page_id = ?", (page_id,))

    def _row_to_attempt(self, row: dict[str, Any]) -> Attempt:
        return Attempt(
            id=row["id"],
            book_id=row["book_id"],
            page_id=row["page_id"],
            block_id=row["block_id"],
            answer=_loads(row.get("answer"), []),
            correct=bool(row.get("correct")),
            created_at=row.get("created_at") or 0.0,
        )
