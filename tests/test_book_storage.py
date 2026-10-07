"""Book 存储：书/页/消息/作答 CRUD 与级联、页与 spine 对齐、每书锁。"""

import pytest

from nnnu.book.models import Attempt, Block, Book, BookPage, PageMessage
from nnnu.book.storage import BookStorage
from nnnu.services.sessions.db import Database
from nnnu.services.sessions.schema import db_path, migrate


@pytest.fixture
async def storage(tmp_home):
    data_root = tmp_home / "data"
    migrate(data_root)
    db = Database(db_path(data_root))
    await db.connect()
    try:
        yield BookStorage(db)
    finally:
        await db.close()


def _book() -> Book:
    return Book.new(title="测试书", sources={"kbs": ["kb-1"]})


def _chapters(*keys: str) -> list[dict]:
    return [{"key": key, "title": f"章 {key}"} for key in keys]


async def test_book_roundtrip_and_update(storage: BookStorage):
    book = await storage.insert_book(_book())
    loaded = await storage.get_book(book.id)
    assert loaded is not None
    assert loaded.title == "测试书"
    # 素材选择按规范化形状整存（空类显式落 []，读端不用猜哪些键存在）
    assert loaded.sources["kbs"] == ["kb-1"]
    assert loaded.sources["notebooks"] == [] and loaded.sources["sessions"] == []
    assert loaded.status == "draft"

    updated = await storage.update_book(
        book.id, spine={"chapters": _chapters("ch-1")}, status="compiling", error="x"
    )
    assert updated is not None
    assert updated.spine["chapters"][0]["key"] == "ch-1"
    assert updated.status == "compiling"
    assert updated.error == "x"
    assert updated.updated_at >= loaded.updated_at

    assert await storage.update_book("bk-不存在", title="x") is None
    assert [b.id for b in await storage.list_books()] == [book.id]


async def test_delete_book_cascades_pages_messages_attempts(storage: BookStorage):
    book = await storage.insert_book(_book())
    pages = await storage.sync_pages(book.id, _chapters("ch-1"))
    page = pages[0]
    await storage.add_message(PageMessage.new(page_id=page.id, role="user", content_md="问"))
    await storage.add_attempt(
        Attempt.new(book_id=book.id, page_id=page.id, block_id="blk-1", answer=["A"], correct=True)
    )

    assert await storage.delete_book(book.id) is True
    assert await storage.get_book(book.id) is None
    assert await storage.list_pages(book.id) == []
    assert await storage.list_messages(page.id) == []
    assert await storage.list_attempts(book.id) == []
    assert await storage.delete_book(book.id) is False


async def test_sync_pages_rename_keeps_page_delete_drops_it(storage: BookStorage):
    book = await storage.insert_book(_book())
    pages = await storage.sync_pages(book.id, _chapters("ch-1", "ch-2", "ch-3"))
    assert [p.page_no for p in pages] == [1, 2, 3]
    # 给第一页塞一个已编译的块；给将被删的 ch-2 页留一条聊天消息
    block = Block.new(block_type="text", payload={"markdown": "旧正文"})
    await storage.update_page_blocks(pages[0].id, [block])
    await storage.add_message(PageMessage.new(page_id=pages[1].id, role="user", content_md="嗨"))

    # 改名（key 不变）→ 页与块都保住；删 ch-2 → 该页连消息一起走；重排 → page_no 重算
    pages2 = await storage.sync_pages(book.id, _chapters("ch-3", "ch-1"))
    assert [(p.chapter_key, p.page_no) for p in pages2] == [("ch-3", 1), ("ch-1", 2)]
    kept = await storage.get_page(book.id, pages[0].id)
    assert kept is not None and kept.blocks[0].payload["markdown"] == "旧正文"
    assert await storage.get_page(book.id, pages[1].id) is None  # ch-2 的页没了
    assert await storage.list_messages(pages[1].id) == []  # 消息随页级联


async def test_get_page_is_scoped_to_book(storage: BookStorage):
    book_a = await storage.insert_book(_book())
    book_b = await storage.insert_book(_book())
    pages = await storage.sync_pages(book_a.id, _chapters("ch-1"))
    assert await storage.get_page(book_b.id, pages[0].id) is None
    assert await storage.get_page(book_a.id, pages[0].id) is not None


async def test_blocks_and_flags_roundtrip(storage: BookStorage):
    book = await storage.insert_book(_book())
    page = (await storage.sync_pages(book.id, _chapters("ch-1")))[0]
    blocks = [
        Block.new(block_type="text", payload={"markdown": "一"}),
        Block.new(
            block_type="quiz",
            payload={
                "stem": "题",
                "options": [{"key": "A", "text": "对"}, {"key": "B", "text": "错"}],
                "answer_key": ["A"],
                "explanation": "",
            },
        ),
    ]
    blocks[0].status = "done"
    await storage.update_page_blocks(page.id, blocks)

    loaded = await storage.get_page(book.id, page.id)
    assert loaded is not None
    assert [b.type for b in loaded.blocks] == ["text", "quiz"]
    assert loaded.blocks[0].status == "done"
    assert loaded.blocks[1].payload["answer_key"] == ["A"]

    await storage.update_page_flags(page.id, visited=True, bookmarked=True)
    loaded = await storage.get_page(book.id, page.id)
    assert loaded is not None and loaded.visited and loaded.bookmarked


async def test_messages_order_and_limit(storage: BookStorage):
    book = await storage.insert_book(_book())
    page = (await storage.sync_pages(book.id, _chapters("ch-1")))[0]
    for index in range(5):
        message = PageMessage.new(page_id=page.id, role="user", content_md=f"第 {index} 条")
        message.created_at = 100.0 + index
        await storage.add_message(message)
    all_messages = await storage.list_messages(page.id)
    assert [m.content_md for m in all_messages] == [f"第 {index} 条" for index in range(5)]
    latest = await storage.list_messages(page.id, limit=2)
    assert [m.content_md for m in latest] == ["第 3 条", "第 4 条"]  # 最近两条，仍正序


async def test_attempts_and_block_cleanup(storage: BookStorage):
    book = await storage.insert_book(_book())
    page = (await storage.sync_pages(book.id, _chapters("ch-1")))[0]
    await storage.add_attempt(
        Attempt.new(book_id=book.id, page_id=page.id, block_id="blk-a", answer=["A"], correct=True)
    )
    await storage.add_attempt(
        Attempt.new(book_id=book.id, page_id=page.id, block_id="blk-b", answer=["B"], correct=False)
    )
    attempts = await storage.list_attempts(book.id)
    assert [(a.block_id, a.correct) for a in attempts] == [("blk-a", True), ("blk-b", False)]

    await storage.delete_block_attempts(page.id, "blk-a")
    assert [a.block_id for a in await storage.list_attempts(book.id)] == ["blk-b"]


async def test_book_lock_is_per_book(storage: BookStorage):
    lock_a1 = storage.book_lock("bk-a")
    lock_a2 = storage.book_lock("bk-a")
    lock_b = storage.book_lock("bk-b")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b
