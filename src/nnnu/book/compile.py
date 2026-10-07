"""编译任务运行器（§7.14）：确认目录后的逐章逐块生成，可暂停、可续跑、可崩后恢复。

形态照 services/knowledge/service.py 的长任务先例（进程内 asyncio.Task + 状态落库 +
wait_idle/shutdown），但状态的唯一写者是任务自己：pause 端点只置每书一个 Event，
任务在块边界看到它才收手——不做任务取消等待，竞态面最小。

几个定死的口径：
- 单书单飞：同一本书同一时刻只跑一个编译任务（重复触发由这里抛 409 compile_busy）；
- 串行：章节按 spine 序、块按页内序**串行**生成——E2E 脚本的 LLM 步骤与这个顺序一一对应；
- 落库节奏：每块开跑前写 compiling、跑完写 done/error（整页数组替换式写）——暂停/中断
  最多丢当前块的进度；
- 坏块不炸整书：BlockGenerationError 记进块的 error 继续；模型侧 LLMError（没配/断网）
  中断整本书（已完成的块保留，再点编译即续跑）；
- 终态一处判：跑完之后还有 pending/compiling 的块 → paused，否则 → ready——
  §14「编译中断可续」就落在这个「没做完就退回可续状态」的口径上；
- recover：进程重启后把 compiling 的书改回 paused、页内 compiling 的块改回 pending。

另托管单块生成（重生成/换类型/动画 202 路径，见 generate_into_block 与
start_block_generation）：块级任务按块 id 单飞，与整书编译互不占位；开编时顺手
拍一次源指纹快照（books.fingerprints，健康检查的比对底本）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from nnnu.book import inputs as book_inputs
from nnnu.book.blocks import BlockGenerationError, generate_block
from nnnu.book.health import take_snapshot
from nnnu.book.models import (
    MATERIAL_BUDGET_CHARS,
    MAX_ERROR_CHARS,
    Block,
    Book,
    BookError,
    BookPage,
    Chapter,
    Spine,
)
from nnnu.book.spine import normalize_language
from nnnu.book.storage import BookStorage
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.llm.errors import LLMError

if TYPE_CHECKING:
    from nnnu.services.cost.service import CostService
    from nnnu.services.knowledge.service import KBService
    from nnnu.services.notebooks.service import NotebookService
    from nnnu.services.question_bank.service import QuestionBankService
    from nnnu.services.sessions.service import SessionManager

logger = logging.getLogger(__name__)


def _spine_of(book: Book) -> Spine:
    try:
        return Spine.model_validate(book.spine)
    except ValidationError:  # 防守：列数据是我们自己写的
        return Spine()


def _demote(blocks: list[Block], statuses: tuple[str, ...]) -> bool:
    """把指定状态的块退回 pending（清 error）；返回有没有改动。"""
    changed = False
    for block in blocks:
        if block.status in statuses:
            block.status = "pending"
            block.error = ""
            block.updated_at = time.time()
            changed = True
    return changed


def _prepare_blocks(page: BookPage, chapter: Chapter, *, regenerate: bool) -> list[Block]:
    """把页内块对齐到本章计划的「开跑状态」。

    - 续跑（页里已有块）：done 原样留着（含用户手写的 note）；中断/失败过的
      （compiling/error）退回 pending 等重来——中断续跑就靠这一条；
    - 首次编译 / 整章重跑（regenerate）：按 blocks_plan 重建（块 id 整批换新，
      旧作答由调用方一起清）；note 是用户手写的，留住不重生成。
    """
    existing = list(page.blocks)
    if not regenerate and existing:
        _demote(existing, ("compiling", "error"))
        return existing
    kept_notes = [block for block in existing if block.type == "note"]
    refs = [ref.model_dump() for ref in chapter.source_refs]
    blocks: list[Block] = []
    for plan in chapter.blocks_plan:
        if plan.type == "note":
            if kept_notes:
                note = kept_notes.pop(0)
            else:
                note = Block.new(
                    block_type="note", payload={"markdown": ""}, focus=plan.focus, source_refs=refs
                )
            note.status = "done"  # note 不生成内容，直接算完成
            blocks.append(note)
            continue
        blocks.append(Block.new(block_type=plan.type, focus=plan.focus, source_refs=refs))
    blocks.extend(kept_notes)  # 计划里没列到的旧手写笔记也留住
    return blocks


class CompileRunner:
    """进程内的编译任务池：{book_id: asyncio.Task}，每书一个暂停 Event。"""

    def __init__(
        self,
        storage: BookStorage,
        *,
        kb: "KBService",
        notebooks: "NotebookService",
        questions: "QuestionBankService",
        sessions: "SessionManager",
        cost: "CostService",
    ) -> None:
        self._storage = storage
        self._kb = kb
        self._notebooks = notebooks
        self._questions = questions
        self._sessions = sessions
        self._cost = cost
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pause: dict[str, asyncio.Event] = {}
        # 单块生成任务（animation 重生成走 202 + 轮询）：key 是块 id，与整书编译互不占位
        self._block_tasks: dict[str, asyncio.Task[None]] = {}

    # ---- 对外：启动 / 暂停 / 等收工 / 关停 / 恢复 ----

    def is_running(self, book_id: str) -> bool:
        task = self._tasks.get(book_id)
        return task is not None and not task.done()

    async def start(self, book_id: str, *, chapters: list[str] | None = None) -> None:
        """编译/续跑（chapters 给 key 列表 = 整章重跑），立即返回，任务后台跑。"""
        book = await self._storage.get_book(book_id)
        if book is None:
            raise BookError("书不存在", code="not_found")
        if self.is_running(book_id) or book.status == "compiling":
            raise BookError("这本书正在编译，先暂停或等它跑完", code="compile_busy")
        spine = _spine_of(book)
        if not spine.chapters:
            raise BookError("这本书还没有章节，先生成目录", code="invalid_spine")
        if chapters is not None:
            if not chapters:
                raise BookError("重跑要至少指定一个章节", code="invalid_request")
            known = {chapter.key for chapter in spine.chapters}
            unknown = [key for key in chapters if key not in known]
            if unknown:
                raise BookError(f"章节 {unknown[0]} 不在目录里", code="unknown_chapter")
        await self._storage.update_book(book_id, status="compiling", error="")
        self._pause[book_id] = asyncio.Event()
        task = asyncio.create_task(self._worker(book_id, chapters), name=f"book-compile-{book_id}")
        self._tasks[book_id] = task
        task.add_done_callback(partial(self._forget, book_id))

    async def pause(self, book_id: str) -> bool:
        """置暂停旗标（任务在下一个块边界收手）；没有任务在跑返回 False（no-op）。"""
        book = await self._storage.get_book(book_id)
        if book is None:
            raise BookError("书不存在", code="not_found")
        event = self._pause.get(book_id)
        if event is None or not self.is_running(book_id):
            return False
        event.set()
        return True

    async def wait_idle(self, book_id: str, timeout: float = 60.0) -> None:
        """等这本书的编译任务收工（测试/关停用）；shield 住超时，别把任务等没了。"""
        task = self._tasks.get(book_id)
        if task is None or task.done():
            return
        await asyncio.wait_for(asyncio.shield(task), timeout)

    async def shutdown(self) -> None:
        """关停：取消所有在跑任务（整书编译 + 单块生成）；任务自己把状态交还给「可续」。"""
        tasks = [
            task for task in (*self._tasks.values(), *self._block_tasks.values()) if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pause.clear()
        self._block_tasks.clear()

    async def recover(self) -> None:
        """启动兜底（§14）：进程被杀时留下的 compiling 书改回 paused、编译中块退回 pending。"""
        for book in await self._storage.list_books():
            if book.status == "compiling":
                logger.info("书籍 %s 上次编译没跑完，改回 paused 供续跑", book.id)
                await self._finalize_interrupted(book.id)
                continue
            # 单块生成（动画重生成）也可能被进程杀掉：ready 的书里照样要退块
            for page in await self._storage.list_pages(book.id):
                if _demote(page.blocks, ("compiling",)):
                    await self._storage.update_page_blocks(page.id, page.blocks)

    # ---- 单块生成（重生成 / 换类型 / 动画块 202 路径）----

    def block_generation_running(self, block_id: str) -> bool:
        task = self._block_tasks.get(block_id)
        return task is not None and not task.done()

    def start_block_generation(self, book_id: str, page_id: str, block_id: str) -> None:
        """animation 单块生成（202 路径）：已经在跑就抛 compile_busy 语义。"""
        if self.block_generation_running(block_id):
            raise BookError("这个动画块正在生成，等它跑完再操作", code="compile_busy")
        task = asyncio.create_task(
            self._block_worker(book_id, page_id, block_id), name=f"book-block-{block_id}"
        )
        self._block_tasks[block_id] = task
        task.add_done_callback(partial(self._forget_block, block_id))

    def _forget_block(self, block_id: str, task: asyncio.Task[None]) -> None:
        if self._block_tasks.get(block_id) is task:
            self._block_tasks.pop(block_id, None)

    async def _block_worker(self, book_id: str, page_id: str, block_id: str) -> None:
        tracker = CostTracker()
        try:
            await self.generate_into_block(book_id, page_id, block_id, tracker=tracker)
        except asyncio.CancelledError:
            # 关停：块退回 pending 等下次重来（防卡在 compiling）
            await self._best_effort(self._demote_block(book_id, page_id, block_id))
            raise
        except Exception:  # 包括 LLMError：块已落 error，任务只负责不静默
            logger.exception("块 %s 生成失败", block_id)
        finally:
            await self._best_effort(self._record_cost(f"book-block-{block_id}", tracker))

    async def _demote_block(self, book_id: str, page_id: str, block_id: str) -> None:
        page = await self._storage.get_page(book_id, page_id)
        if page is None:
            return
        block = next((item for item in page.blocks if item.id == block_id), None)
        if block is not None and _demote([block], ("compiling",)):
            await self._storage.update_page_blocks(page_id, page.blocks)

    async def generate_into_block(
        self, book_id: str, page_id: str, block_id: str, *, tracker: CostTracker | None = None
    ) -> Block:
        """给单块跑一次生成并写回（重生成/换类型/插入后生成公用）。

        compiling → generate → done/error 全程落库：BlockGenerationError 记在块上返回
        （内容问题，界面显示块的错误）；LLMError 记在块上后**再抛**（路由 503，用户重试）——
        两条路径块都不会卡在 compiling。
        """
        tracker = tracker or CostTracker()
        book = await self._storage.get_book(book_id)
        page = await self._storage.get_page(book_id, page_id)
        if book is None or page is None:
            raise BookError("书或页不存在", code="not_found")
        block = next((item for item in page.blocks if item.id == block_id), None)
        if block is None:
            raise BookError("块不存在", code="not_found")
        spine = _spine_of(book)
        try:
            chapter = next(item for item in spine.chapters if item.key == page.chapter_key)
        except StopIteration:  # 章节被删过：按无章节信息生成（focus 还在块上）
            chapter = Chapter(key=page.chapter_key, title=page.chapter_key)
        language = normalize_language(spine.language)
        share = MATERIAL_BUDGET_CHARS // max(1, len(spine.chapters))
        material = await self._chapter_material(book, chapter, language, share)

        block.status = "compiling"
        block.error = ""
        block.updated_at = time.time()
        await self._storage.update_page_blocks(page.id, page.blocks)
        try:
            payload = await generate_block(
                block_type=block.type,
                chapter=chapter,
                focus=block.focus,
                material_text=material,
                graph_seed=spine.concept_graph.model_dump(),
                language=language,
                tracker=tracker,
            )
        except BlockGenerationError as exc:
            block.status = "error"
            block.error = str(exc)[:MAX_ERROR_CHARS]
            block.updated_at = time.time()
            await self._storage.update_page_blocks(page.id, page.blocks)
            return block
        except LLMError as exc:
            block.status = "error"
            block.error = str(exc)[:MAX_ERROR_CHARS]
            block.updated_at = time.time()
            await self._storage.update_page_blocks(page.id, page.blocks)
            raise
        block.status = "done"
        block.payload = payload
        block.error = ""
        block.updated_at = time.time()
        await self._storage.update_page_blocks(page.id, page.blocks)
        return block

    # ---- 任务体 ----

    def _forget(self, book_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(book_id) is task:  # 别踩后来者的登记
            self._tasks.pop(book_id, None)
            self._pause.pop(book_id, None)

    async def _worker(self, book_id: str, chapters: list[str] | None) -> None:
        tracker = CostTracker()
        try:
            await self._run_book(book_id, chapters, tracker)
        except asyncio.CancelledError:
            await self._best_effort(self._finalize_interrupted(book_id))
            raise
        except LLMError as exc:
            # 模型没配/调用失败：整本书停下（已完成的块保留，再点编译即续跑）
            logger.warning("书籍 %s 编译中断（模型侧）：%s", book_id, exc)
            await self._best_effort(self._finalize_interrupted(book_id, error=str(exc)))
        except Exception as exc:  # 其余意外：落 error 不静默丢
            logger.exception("书籍 %s 编译失败", book_id)
            await self._best_effort(self._finalize_interrupted(book_id, error=str(exc)))
        else:
            status = await self._final_status(book_id)
            await self._best_effort(self._storage.update_book(book_id, status=status))
        finally:
            await self._best_effort(self._record_cost(f"book-compile-{book_id}", tracker))

    async def _run_book(
        self, book_id: str, chapters: list[str] | None, tracker: CostTracker
    ) -> None:
        book = await self._storage.get_book(book_id)
        if book is None:
            return
        # 编译开始时拍源指纹快照（健康检查的比对底本；拍不动就当没有，不拖垮编译）
        await self._best_effort(self._store_snapshot(book))
        spine = _spine_of(book)
        language = normalize_language(spine.language)
        regenerate = chapters is not None
        wanted_keys = set(chapters) if chapters is not None else None
        wanted = [
            chapter
            for chapter in spine.chapters
            if wanted_keys is None or chapter.key in wanted_keys
        ]
        share = MATERIAL_BUDGET_CHARS // max(1, len(spine.chapters))
        graph_seed = spine.concept_graph.model_dump()

        # 先把要编的页全部对齐到块计划并落库——进度面板一开始就能看到总块数
        prepared: list[tuple[Chapter, BookPage, list[Block]]] = []
        for chapter in wanted:
            page = await self._storage.get_page_by_chapter(book_id, chapter.key)
            if page is None:
                continue
            blocks = _prepare_blocks(page, chapter, regenerate=regenerate)
            await self._storage.update_page_blocks(page.id, blocks)
            if regenerate:
                # 整章重跑：块 id 整批换新，旧作答对不上任何块了
                await self._storage.delete_page_attempts(page.id)
            prepared.append((chapter, page, blocks))

        event = self._pause.get(book_id)
        for chapter, page, blocks in prepared:
            material = await self._chapter_material(book, chapter, language, share)
            for block in blocks:
                if block.status == "done":
                    continue
                if event is not None and event.is_set():
                    return  # 收手：剩下的块等续跑
                block.status = "compiling"
                block.updated_at = time.time()
                await self._storage.update_page_blocks(page.id, blocks)
                try:
                    payload = await generate_block(
                        block_type=block.type,
                        chapter=chapter,
                        focus=block.focus,
                        material_text=material,
                        graph_seed=graph_seed,
                        language=language,
                        tracker=tracker,
                    )
                except BlockGenerationError as exc:
                    # 坏块不炸整书：记下来继续编译别的
                    block.status = "error"
                    block.error = str(exc)[:MAX_ERROR_CHARS]
                else:
                    block.status = "done"
                    block.payload = payload
                    block.error = ""
                block.updated_at = time.time()
                await self._storage.update_page_blocks(page.id, blocks)

    async def _chapter_material(
        self, book: Book, chapter: Chapter, language: str, share: int
    ) -> str:
        """按章汇集素材正文；素材侧出毛病按「没素材」继续，别拖垮整本书。"""
        try:
            digest = await book_inputs.collect_chapter_material(
                book.sources,
                chapter,
                kb_service=self._kb,
                notebook_service=self._notebooks,
                question_service=self._questions,
                session_manager=self._sessions,
                language=language,
                budget_chars=share,
            )
        except Exception:  # 素材是锦上添花，检索炸了也照写
            logger.exception("章节 %s 的素材汇集失败，按无素材继续", chapter.key)
            return ""
        return digest.text

    async def _final_status(self, book_id: str) -> str:
        for page in await self._storage.list_pages(book_id):
            if any(block.status in ("pending", "compiling") for block in page.blocks):
                return "paused"
        return "ready"

    async def _finalize_interrupted(self, book_id: str, *, error: str = "") -> None:
        """中断收尾：书退到 paused（模型侧错误则 error），页内 compiling 的块退回 pending。"""
        status = "error" if error else "paused"
        await self._storage.update_book(book_id, status=status, error=error[:MAX_ERROR_CHARS])
        for page in await self._storage.list_pages(book_id):
            if _demote(page.blocks, ("compiling",)):
                await self._storage.update_page_blocks(page.id, page.blocks)

    async def _store_snapshot(self, book: Book) -> None:
        snapshot = await take_snapshot(
            book.sources,
            kb_service=self._kb,
            notebook_service=self._notebooks,
            question_service=self._questions,
            session_manager=self._sessions,
        )
        await self._storage.update_book(book.id, fingerprints=snapshot)

    async def _record_cost(self, turn_id: str, tracker: CostTracker) -> None:
        summary = tracker.summary()
        if summary.get("per_model"):
            await self._cost.record_turn(session_id="", turn_id=turn_id, summary=summary)

    async def _best_effort(self, awaitable: Any) -> None:
        try:
            await awaitable
        except Exception:  # 收尾动作不许盖掉真异常 / 打断关停
            logger.exception("编译任务收尾动作失败")
