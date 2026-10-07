"""Book 服务（§7.14）：书的增删改查 + spine 生成编排 + 编译触发 + 页/块/进度/导出。

骨架照 Co-Writer：路由只做请求体封口与错误映射，真正的编排在这里；单例
经 get_book_service() 取（lifespan 装配，路由与编译任务共用一份）。

创建流程一次同步走完「落书 → 汇素材 → 跑 spine → 建页」：中间任何一步失败
（素材不可用 / 模型没配 / 输出洗不出来）都把刚落的书删掉再抛——不留一本没
spine 的孤儿书（要重来就重新建，素材选择在前端还留着）。

页/块的读写口径：
- 编译中（任务在跑或状态是 compiling）一律拒块编辑与删除（409 compile_busy）——
  单写者原则：状态的写者要么是编译任务，要么是这里，绝不同时；
- 块编辑六种 op 里，regenerate/retype 同步生成并返回（客户端放宽超时），
  只有 animation 走 202（真长任务，前端轮询页详情）；
- 页聊天按页一把锁：同一页同时只答一问（忙态 409 chat_busy）；用户消息先落库、
  失败也留着（读者回头能看到自己问过什么）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from nnnu.book import inputs as book_inputs
from nnnu.book.blocks import FENCE_BLOCK_TYPES, fence_reason
from nnnu.book.chat import PAGE_CHAT_HISTORY, run_page_chat
from nnnu.book.compile import CompileRunner
from nnnu.book.estimate import estimate_book
from nnnu.book.export import export_book_markdown
from nnnu.book.health import check_drift
from nnnu.book.models import (
    BLOCK_TYPES,
    Attempt,
    Block,
    Book,
    BookError,
    BookPage,
    Chapter,
    PageMessage,
    Spine,
    spine_issues,
    validate_block_payload,
    validate_chapter_title,
    validate_title,
)
from nnnu.book.progress import (
    compute_progress,
    latest_by_block,
    normalize_answer,
    score_quiz,
)
from nnnu.book.spine import generate_spine, normalize_language
from nnnu.book.storage import BookStorage
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.llm.factory import resolve_model_config
from nnnu.services.sessions.db import Database

if TYPE_CHECKING:
    from nnnu.services.cost.service import CostService
    from nnnu.services.knowledge.service import KBService
    from nnnu.services.notebooks.service import NotebookService
    from nnnu.services.question_bank.service import QuestionBankService
    from nnnu.services.sessions.service import SessionManager

logger = logging.getLogger(__name__)


class BookService:
    def __init__(
        self,
        db: Database,
        *,
        kb: "KBService",
        notebooks: "NotebookService",
        questions: "QuestionBankService",
        sessions: "SessionManager",
        cost: "CostService",
    ) -> None:
        self._storage = BookStorage(db)
        self._kb = kb
        self._notebooks = notebooks
        self._questions = questions
        self._sessions = sessions
        self._cost = cost
        self._chat_locks: dict[str, asyncio.Lock] = {}  # 每页一把（页聊天忙态）
        self._compile = CompileRunner(
            self._storage,
            kb=kb,
            notebooks=notebooks,
            questions=questions,
            sessions=sessions,
            cost=cost,
        )

    # ---- 列表 / 详情 ----

    async def list_books(self) -> list[dict[str, Any]]:
        """书库列表：每本带进度摘要（个人规模下逐本现算，够快）。"""
        books = await self._storage.list_books()
        result = []
        for book in books:
            pages = await self._storage.list_pages(book.id)
            attempts = await self._storage.list_attempts(book.id)
            result.append({**self._summary(book), "progress": compute_progress(pages, attempts)})
        return result

    async def book_detail(self, book_id: str) -> dict[str, Any] | None:
        """GET 详情（前端轮询用）：spine + 估算 + 页 meta + 进度 + 健康漂移。"""
        book = await self._storage.get_book(book_id)
        if book is None:
            return None
        spine = self._spine_of(book)
        pages = await self._storage.list_pages(book_id)
        return {
            **book.model_dump(),
            # 警示是「现算」的：改了目录（比如把唯一的 flashcard 删了）立刻反映
            "issues": spine_issues(book.spine),
            "estimate": estimate_book(spine, model=self._model_name()),
            "pages": [self._page_meta(page) for page in pages],
            "progress": compute_progress(pages, await self._storage.list_attempts(book_id)),
            "health": await self._health(book),
        }

    async def _health(self, book: Book) -> dict[str, Any] | None:
        """源漂移报告：只对 ready 且拍过快照的书现算；编译中轮询不做无用功。"""
        if book.status != "ready" or not book.fingerprints:
            return None
        try:
            return await check_drift(
                book.fingerprints,
                book.sources,
                kb_service=self._kb,
                notebook_service=self._notebooks,
                question_service=self._questions,
            )
        except Exception:  # 健康检查是锦上添花，读源出毛病不能连坐书详情
            logger.exception("书籍 %s 健康检查失败", book.id)
            return None

    def _summary(self, book: Book) -> dict[str, Any]:
        spine = self._spine_of(book)
        return {
            **book.model_dump(),
            "chapter_count": len(spine.chapters),
            "issues": spine_issues(book.spine),
        }

    def _page_meta(self, page: BookPage) -> dict[str, Any]:
        return {
            "id": page.id,
            "chapter_key": page.chapter_key,
            "page_no": page.page_no,
            "visited": page.visited,
            "bookmarked": page.bookmarked,
            "block_count": len(page.blocks),
            "blocks_done": sum(1 for block in page.blocks if block.status == "done"),
            "blocks_error": sum(1 for block in page.blocks if block.status == "error"),
            # 编译看板要逐块显状态：只给三件（payload 太重，走页详情）
            "blocks": [
                {"id": block.id, "type": block.type, "status": block.status}
                for block in page.blocks
            ],
        }

    def _spine_of(self, book: Book) -> Spine:
        try:
            return Spine.model_validate(book.spine)
        except ValidationError:  # 防守：列数据是我们自己写的，理论上不会坏
            return Spine()

    def _model_name(self) -> str:
        try:
            return resolve_model_config().model
        except Exception:  # 模型没配全不影响看书：估算退成只报 token 数
            return ""

    # ---- 创建 / 修改 / 删除 ----

    async def create_book(self, *, title: str, sources: Any, language: str = "zh") -> Book:
        book = Book.new(title=title, sources=sources)
        lang = normalize_language(language)
        await self._storage.insert_book(book)
        try:
            digest = await book_inputs.collect_materials(
                book.sources,
                kb_service=self._kb,
                notebook_service=self._notebooks,
                question_service=self._questions,
                session_manager=self._sessions,
                language=lang,
            )
            if not digest.items:
                raise BookError(
                    "勾选的素材现在一份都用不上（库还没就绪 / 记录还是空的）",
                    code="sources_unavailable",
                )
            tracker = CostTracker()
            outcome = await generate_spine(
                title=book.title, digest=digest, language=lang, tracker=tracker
            )
            if outcome.issues:
                logger.info("book %s spine 收编记录：%s", book.id, "；".join(outcome.issues))
            updated = await self._storage.update_book(book.id, spine=outcome.spine, status="draft")
            await self._storage.sync_pages(
                book.id, [chapter.model_dump() for chapter in outcome.spine.chapters]
            )
            await self._record_turn(book.id, "spine", tracker)
            return updated or book
        except Exception:
            await self._storage.delete_book(book.id)  # 失败不留孤儿书
            raise

    async def update_book(
        self,
        book_id: str,
        *,
        title: str | None = None,
        chapters: list[dict[str, str]] | None = None,
    ) -> Book:
        """改书名 / 改目录。chapters 是**全量有序列表**（[{key,title}]）：改名靠新
        title，删章靠不带 key，排序靠数组顺序；其余字段（块计划/来源引用）原样保留。
        """
        book = await self._require(book_id)
        if chapters is not None:
            if book.status != "draft":
                raise BookError("目录只能在开始编译前改", code="not_draft")
            spine = self._rename_chapters(self._spine_of(book), chapters)
            await self._storage.update_book(book_id, spine=spine)
            await self._storage.sync_pages(
                book_id, [chapter.model_dump() for chapter in spine.chapters]
            )
        if title is not None:
            await self._storage.update_book(book_id, title=validate_title(title))
        return await self._require(book_id)

    def _rename_chapters(self, spine: Spine, patches: list[dict[str, str]]) -> Spine:
        by_key = {chapter.key: chapter for chapter in spine.chapters}
        kept: list[Chapter] = []
        seen: set[str] = set()
        for patch in patches:
            key = str(patch.get("key") or "")
            chapter = by_key.get(key)
            if chapter is None:
                raise BookError(f"章节 {key or '?'} 不在当前目录里", code="unknown_chapter")
            if key in seen:
                continue
            seen.add(key)
            kept.append(
                chapter.model_copy(
                    update={"title": validate_chapter_title(str(patch.get("title") or ""))}
                )
            )
        if not kept:
            raise BookError("至少要保留一章", code="invalid_spine")
        return spine.model_copy(update={"chapters": kept})

    async def delete_book(self, book_id: str) -> bool:
        book = await self._storage.get_book(book_id)
        if book is None:
            return False
        if book.status == "compiling":
            raise BookError("编译中不能删书，先暂停编译", code="compile_busy")
        return await self._storage.delete_book(book_id)

    # ---- 编译（任务在 CompileRunner，这里只是门面；lifespan 负责 recover/shutdown）----

    async def compile_book(self, book_id: str, *, chapters: list[str] | None = None) -> None:
        """确认目录后开编 / 暂停后续跑；chapters 给 key 列表 = 整章重跑。"""
        await self._compile.start(book_id, chapters=chapters)

    async def pause_compile(self, book_id: str) -> bool:
        """请求暂停（任务在下一个块边界收手）；没有任务在跑返回 False。"""
        return await self._compile.pause(book_id)

    def compile_running(self, book_id: str) -> bool:
        return self._compile.is_running(book_id)

    async def wait_compile_idle(self, book_id: str, timeout: float = 60.0) -> None:
        """等这本书的编译任务收工（测试与关停用）。"""
        await self._compile.wait_idle(book_id, timeout)

    async def recover_compile(self) -> None:
        """启动兜底：把上次没跑完的 compiling 书改回可续（paused）。"""
        await self._compile.recover()

    async def shutdown(self) -> None:
        """关停：先收编编译任务（任务自己把状态退回可续），再让 lifespan 关库。"""
        await self._compile.shutdown()

    # ---- 页 / 块编辑 / 页聊天 / 作答 / 导出 ----

    async def get_page_detail(self, book_id: str, page_id: str) -> dict[str, Any] | None:
        """整页（阅读器用）：块含 payload + 页聊天历史 + 本章元信息 + 每块最近作答。

        作答只给每块**最近一次**（前端刷新/翻页回来还要显示答没答对；历史次数
        对阅读器没用，进度口径后端自己按全量历史算）。
        """
        book = await self._storage.get_book(book_id)
        if book is None:
            return None
        page = await self._storage.get_page(book_id, page_id)
        if page is None:
            return None
        chapter = self._chapter_of(book, page)
        messages = await self._storage.list_messages(page_id)
        latest = latest_by_block(
            [
                attempt
                for attempt in await self._storage.list_attempts(book_id)
                if attempt.page_id == page_id
            ]
        )
        return {
            "id": page.id,
            "book_id": page.book_id,
            "chapter_key": page.chapter_key,
            "page_no": page.page_no,
            "visited": page.visited,
            "bookmarked": page.bookmarked,
            "updated_at": page.updated_at,
            "chapter": chapter.model_dump(),
            "blocks": [block.model_dump() for block in page.blocks],
            "messages": [message.model_dump() for message in messages],
            "attempts": [
                {"block_id": attempt.block_id, "answer": attempt.answer, "correct": attempt.correct}
                for attempt in latest.values()
            ],
        }

    async def edit_blocks(
        self,
        book_id: str,
        page_id: str,
        *,
        op: str,
        block_id: str | None = None,
        block_type: str | None = None,
        payload: dict[str, Any] | None = None,
        focus: str | None = None,
        direction: str | None = None,
        after_block_id: str | None = None,
    ) -> dict[str, Any]:
        """块编辑六种操作（§9 表 move|update|delete|insert|regenerate|retype）。

        - 编译中一律 409（先暂停再改；不做删除墓碑）；
        - regenerate/retype 同步生成并返回块数组；**animation 走 202**——返回
          {"started": true, "block_id": …}，前端轮询页详情看块状态；
        - note 是手写块：insert/retype 成 note = 白稿直接成稿，regenerate 没有可生成的内容；
        - update/delete 会清该块的作答（题面或块都没了，旧作答对不上）。
        """
        book = await self._require(book_id)
        if self._compile.is_running(book_id) or book.status == "compiling":
            raise BookError("编译中不能改块，先暂停编译", code="compile_busy")
        page = await self._storage.get_page(book_id, page_id)
        if page is None:
            raise BookError("页不存在", code="not_found")
        blocks = list(page.blocks)

        if op == "move":
            block = self._find_block(blocks, block_id)
            if direction not in ("up", "down"):
                raise BookError("move 要给 direction（up/down）", code="invalid_request")
            index = blocks.index(block)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(blocks):  # 到头了就原样返回，前端按钮本来就禁用
                blocks[index], blocks[target] = blocks[target], blocks[index]
            await self._storage.update_page_blocks(page_id, blocks)
        elif op == "update":
            block = self._find_block(blocks, block_id)
            if payload is None:
                raise BookError("update 要给 payload", code="invalid_request")
            self._validate_edit_payload(block.type, payload)
            block.payload = validate_block_payload(block.type, payload)
            block.status = "done"  # 用户亲手写的即成品
            block.error = ""
            block.updated_at = time.time()
            await self._storage.delete_block_attempts(page_id, block.id)
            await self._storage.update_page_blocks(page_id, blocks)
        elif op == "delete":
            block = self._find_block(blocks, block_id)
            blocks.remove(block)
            await self._storage.delete_block_attempts(page_id, block.id)
            await self._storage.update_page_blocks(page_id, blocks)
        elif op == "insert":
            block = self._insert_block(blocks, block_type, payload, focus, after_block_id)
            await self._storage.update_page_blocks(page_id, blocks)
        elif op in ("regenerate", "retype"):
            block = self._find_block(blocks, block_id)
            if op == "retype":
                new_type = str(block_type or "")
                if new_type not in BLOCK_TYPES:
                    raise BookError(
                        f"未知块类型 {new_type!r}，允许：{'、'.join(BLOCK_TYPES)}",
                        code="invalid_block_type",
                    )
                block.type = new_type
                block.payload = {"markdown": ""} if new_type == "note" else {}
                await self._storage.delete_block_attempts(page_id, block.id)
            if focus is not None:
                block.focus = str(focus).strip()[:200]
            if block.type == "note":
                if op == "regenerate":
                    raise BookError("手写笔记块没有可重新生成的内容", code="invalid_request")
                block.status = "done"
                block.error = ""
                block.updated_at = time.time()
                await self._storage.update_page_blocks(page_id, blocks)
            elif block.type == "animation":
                block.status = "compiling"
                block.error = ""
                block.updated_at = time.time()
                await self._storage.update_page_blocks(page_id, blocks)
                self._compile.start_block_generation(book_id, page_id, block.id)
                return {"started": True, "block_id": block.id}
            else:
                await self._storage.update_page_blocks(page_id, blocks)  # 先落 type/focus 改动
                await self._compile.generate_into_block(book_id, page_id, block.id)
        else:
            raise BookError(
                f"未知操作 {op!r}，允许：move、update、delete、insert、regenerate、retype",
                code="invalid_request",
            )

        fresh = await self._storage.get_page(book_id, page_id)
        return {"blocks": [item.model_dump() for item in (fresh.blocks if fresh else [])]}

    def _insert_block(
        self,
        blocks: list[Block],
        block_type: str | None,
        payload: dict[str, Any] | None,
        focus: str | None,
        after_block_id: str | None,
    ) -> Block:
        """插入一个块：非 note 建好是 pending（前端随后调 regenerate 生成，或用户手写后 update）。"""
        new_type = str(block_type or "")
        if new_type == "note":
            block = Block.new(
                block_type="note", payload=payload or {"markdown": ""}, focus=focus or ""
            )
            block.status = "done"  # 手写块建好即成稿，用户随后 edit
        else:
            block = Block.new(block_type=new_type, payload=payload, focus=focus or "")
        anchor = next((item for item in blocks if item.id == after_block_id), None)
        position = blocks.index(anchor) + 1 if anchor is not None else len(blocks)
        blocks.insert(position, block)
        return block

    def _validate_edit_payload(self, block_type: str, payload: Any) -> None:
        """用户改稿也过 figure 家族的深校验（围栏对不上/标签带禁字符会炸读者端渲染）。"""
        if block_type not in FENCE_BLOCK_TYPES:
            return
        markdown = payload.get("markdown") if isinstance(payload, dict) else None
        if not isinstance(markdown, str):
            return  # 形状问题交给 validate_block_payload 统一报
        reason = fence_reason(block_type, markdown)
        if reason is not None:
            raise BookError(reason, code="invalid_payload")

    async def chat_page(self, book_id: str, page_id: str, message: str) -> dict[str, Any]:
        """页聊天（§9 非流式）：历史自存本页，用量按 bookchat-<msg_id> 记账。

        用户消息先落库、失败也留着；同一页同时只答一问（忙态 409 chat_busy）。
        """
        book = await self._require(book_id)
        page = await self._storage.get_page(book_id, page_id)
        if page is None:
            raise BookError("页不存在", code="not_found")
        question = (message or "").strip()
        if not question:
            raise BookError("问题不能为空", code="invalid_request")
        lock = self._chat_lock(page_id)
        if lock.locked():
            raise BookError("这一页正在回答上一个问题，稍等一下", code="chat_busy")
        async with lock:
            history = [
                {"role": item.role, "content": item.content_md}
                for item in await self._storage.list_messages(page_id, limit=PAGE_CHAT_HISTORY)
            ]
            await self._storage.add_message(
                PageMessage.new(page_id=page_id, role="user", content_md=question)
            )
            outcome = await run_page_chat(
                page=page,
                chapter=self._chapter_of(book, page),
                question=question,
                history=history,
                language=self._spine_of(book).language,
                kb_ids=list(book.sources.get("kbs") or []),
            )
            assistant = PageMessage.new(
                page_id=page_id,
                role="assistant",
                content_md=outcome.answer,
                citations=outcome.citations,
            )
            await self._storage.add_message(assistant)
        if outcome.usage.get("per_model"):
            await self._cost.record_turn(
                session_id="", turn_id=f"bookchat-{assistant.id}", summary=outcome.usage
            )
        return {
            "answer": outcome.answer,
            "citations": outcome.citations,
            "message_id": assistant.id,
            "degraded": outcome.degraded,
            "model": outcome.model,
        }

    async def set_page_flags(
        self,
        book_id: str,
        page_id: str,
        *,
        visited: bool | None = None,
        bookmarked: bool | None = None,
    ) -> dict[str, bool]:
        """阅读进度点：进入页置 visited、书签开关；返回更新后的两个标记。"""
        if visited is None and bookmarked is None:
            raise BookError("没有要改的字段", code="invalid_request")
        if await self._storage.get_page(book_id, page_id) is None:
            raise BookError("页不存在", code="not_found")
        await self._storage.update_page_flags(page_id, visited=visited, bookmarked=bookmarked)
        page = await self._storage.get_page(book_id, page_id)
        return {
            "visited": bool(page is not None and page.visited),
            "bookmarked": bool(page is not None and page.bookmarked),
        }

    async def answer_attempt(
        self, book_id: str, page_id: str, *, block_id: str, answer: Any
    ) -> dict[str, Any]:
        """测验作答：零 LLM 判分（选项 key 集合比较），落库并返回对错与解析。"""
        await self._require(book_id)
        page = await self._storage.get_page(book_id, page_id)
        if page is None:
            raise BookError("页不存在", code="not_found")
        block = next((item for item in page.blocks if item.id == block_id), None)
        if block is None:
            raise BookError("块不存在", code="not_found")
        if block.type != "quiz" or block.status != "done":
            raise BookError("这个块不是已生成的测验", code="invalid_block")
        keys = normalize_answer(answer)
        if not keys:
            raise BookError("至少选一个选项", code="invalid_request")
        correct = score_quiz(block, keys)
        attempt = Attempt.new(
            book_id=book_id, page_id=page_id, block_id=block_id, answer=keys, correct=correct
        )
        await self._storage.add_attempt(attempt)
        return {
            "correct": correct,
            "explanation": str(block.payload.get("explanation") or ""),
            "attempt_id": attempt.id,
        }

    async def export_book(self, book_id: str, *, language: str = "zh") -> str | None:
        """整书 Markdown（书不存在回 None；没编完的块导出时自然跳过）。"""
        book = await self._storage.get_book(book_id)
        if book is None:
            return None
        pages = await self._storage.list_pages(book_id)
        return export_book_markdown(book, self._spine_of(book), pages, language)

    # ---- 小工具 ----

    def _chapter_of(self, book: Book, page: BookPage) -> Chapter:
        """页对应的章节元信息；目录被删过就按 key 兜个空壳（不炸阅读器）。"""
        for chapter in self._spine_of(book).chapters:
            if chapter.key == page.chapter_key:
                return chapter
        return Chapter(key=page.chapter_key, title=page.chapter_key)

    def _find_block(self, blocks: list[Block], block_id: str | None) -> Block:
        block = next((item for item in blocks if item.id == block_id), None)
        if block is None:
            raise BookError("块不存在", code="not_found")
        return block

    def _chat_lock(self, page_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(page_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[page_id] = lock
        return lock

    async def _require(self, book_id: str) -> Book:
        book = await self._storage.get_book(book_id)
        if book is None:
            raise BookError("书不存在", code="not_found")
        return book

    async def _record_turn(self, book_id: str, kind: str, tracker: CostTracker) -> None:
        """记账：沿用合成 turn_id 先例（不建会话、不写消息，只把成本挂上）。"""
        summary = tracker.summary()
        if summary.get("per_model"):
            await self._cost.record_turn(
                session_id="", turn_id=f"book-{kind}-{book_id}", summary=summary
            )


_book_service: BookService | None = None


def get_book_service() -> BookService:
    if _book_service is None:
        raise RuntimeError("Book 服务未装配（lifespan 未调用 set_book_service）")
    return _book_service


def set_book_service(service: BookService | None) -> None:
    global _book_service
    _book_service = service
