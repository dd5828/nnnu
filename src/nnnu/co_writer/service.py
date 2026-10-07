"""Co-Writer 服务（§7.13）：文档 CRUD + 改写编排（发起/确认/拒绝）。

元数据表（co_writer_docs）是「文档存不存在」的唯一依据；正文文件是内容载体
（见 storage.py）。改写编排的两次关键动作：
- 发起：读正文快照 → 切片校验（要跟用户看到的正文对齐，对不上就是 409
  doc_changed）→ LLM 在锁外跑 → 待确认编辑（含发起时 digest）装进内存货架；
- 确认：锁内「读当前正文 → 比 digest → 切片替换 → 原子写」——自动保存与写回
  撞车时靠这把锁与 digest 双重兜底；写入的是模型给的 edited 原文，不回放 diff。
"""

import time
from pathlib import Path
from typing import Any

from nnnu.co_writer.edit import run_edit
from nnnu.co_writer.models import (
    CONTEXT_CHARS,
    CoWriterDoc,
    CoWriterError,
    content_digest,
    is_valid_doc_id,
    preview_of,
    title_of,
    validate_content,
    validate_selection,
)
from nnnu.co_writer.storage import CoWriterStorage, PendingEdit, PendingEditStore
from nnnu.core.ids import new_id
from nnnu.services.sessions.db import Database


class CoWriterService:
    def __init__(self, db: Database, *, data_root: Path) -> None:
        self._db = db
        self._storage = CoWriterStorage(data_root)
        self._pending = PendingEditStore()

    # ---- 文档 CRUD ----

    async def list_docs(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM co_writer_docs ORDER BY updated_at DESC, rowid DESC"
        )
        docs = []
        for row in rows:
            doc = self._row_to_doc(row)
            # 预览只读文件头，不整篇读盘（列表可能挂着几十篇大文档）
            preview = preview_of(self._storage.head(doc.id))
            docs.append({**doc.model_dump(), "preview": preview})
        return docs

    async def get_meta(self, doc_id: str) -> CoWriterDoc | None:
        if not is_valid_doc_id(doc_id):
            return None
        row = await self._db.fetch_one("SELECT * FROM co_writer_docs WHERE id = ?", (doc_id,))
        return self._row_to_doc(row) if row else None

    async def get_doc(self, doc_id: str) -> tuple[CoWriterDoc, str] | None:
        meta = await self.get_meta(doc_id)
        if meta is None:
            return None
        content = self._storage.load(doc_id)
        if content is None:  # get_meta 已卡过 id 形状，这里只是防守
            return None
        return meta, content

    async def create_doc(self, *, title: str = "", content: str = "") -> CoWriterDoc:
        validate_content(content)
        doc = CoWriterDoc.new(title=title_of(title, content))
        self._storage.save(doc.id, content)
        try:
            await self._db.execute(
                "INSERT INTO co_writer_docs (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (doc.id, doc.title, doc.created_at, doc.updated_at),
            )
        except Exception:
            self._storage.delete(doc.id)  # 行没落成，别留孤儿文件
            raise
        return doc

    async def update_doc(
        self,
        doc_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
    ) -> CoWriterDoc | None:
        """None = 不改；title 传空串 = 按正文首行重取；改内容自动保存走这条。"""
        meta = await self.get_meta(doc_id)
        if meta is None:
            return None
        if content is not None:
            validate_content(content)
            async with self._storage.lock_for(doc_id):
                self._storage.save(doc_id, content)
        new_title = meta.title
        if title is not None:
            current = content if content is not None else (self._storage.load(doc_id) or "")
            new_title = title_of(title, current)
        await self._db.execute(
            "UPDATE co_writer_docs SET title = ?, updated_at = ? WHERE id = ?",
            (new_title, _now(), doc_id),
        )
        return await self.get_meta(doc_id)

    async def delete_doc(self, doc_id: str) -> bool:
        if await self.get_meta(doc_id) is None:
            return False
        self._storage.delete(doc_id)
        await self._db.execute("DELETE FROM co_writer_docs WHERE id = ?", (doc_id,))
        return True

    # ---- 改写编排 ----

    async def start_edit(
        self,
        doc_id: str,
        *,
        start: int,
        end: int,
        original: str,
        action: str,
        instruction: str = "",
        language: str = "zh",
        kb_ids: list[str] | None = None,
        use_web: bool = False,
    ) -> dict[str, Any] | None:
        """发起一次改写：校验切片 → 跑管线 → 装进待确认货架。文档不存在返回 None。"""
        found = await self.get_doc(doc_id)
        if found is None:
            return None
        _, content = found
        validate_selection(content, start, end, original)
        outcome = await run_edit(
            doc_id=doc_id,
            selection=original,
            prefix=content[max(0, start - CONTEXT_CHARS) : start],
            suffix=content[end : end + CONTEXT_CHARS],
            action=action,
            instruction=instruction,
            language=language,
            kb_ids=kb_ids or [],
            use_web=use_web,
        )
        edit = PendingEdit(
            edit_id=new_id("cwe"),
            doc_id=doc_id,
            start=start,
            end=end,
            original=original,
            edited=outcome.edited,
            digest=content_digest(content),
        )
        self._pending.put(edit)
        return {
            "edit_id": edit.edit_id,
            "original": original,
            "edited": outcome.edited,
            "ops": outcome.ops,
            "stats": outcome.stats,
            "trace": outcome.trace,
            "citations": outcome.citations,
            "usage": outcome.usage,
            "degraded": outcome.degraded,
            "model": outcome.model,
        }

    async def resolve_edit(self, doc_id: str, edit_id: str, action: str) -> dict[str, Any] | None:
        """确认或拒绝一次改写。确认 = 锁内 digest 复核后把 edited 原文写回。"""
        pending = self._pending.pop(edit_id)
        if pending is None or pending.doc_id != doc_id:
            raise CoWriterError(
                "这次改写已过期（超过 30 分钟或服务重启过），请重新发起", code="edit_expired"
            )
        if action == "reject":
            return {"rejected": edit_id}
        # accept：文档删了/内容变了都不给写回
        async with self._storage.lock_for(doc_id):
            content = self._storage.load(doc_id)
            if content is None or await self.get_meta(doc_id) is None:
                return None
            if content_digest(content) != pending.digest:
                raise CoWriterError(
                    "文档内容已经变过，这次改写不能再应用，请重新发起", code="doc_changed"
                )
            updated = content[: pending.start] + pending.edited + content[pending.end :]
            self._storage.save(doc_id, updated)
        await self._db.execute(
            "UPDATE co_writer_docs SET updated_at = ? WHERE id = ?",
            (_now(), doc_id),
        )
        meta = await self.get_meta(doc_id)
        return {"doc": meta.model_dump() if meta else None, "applied": True}

    @staticmethod
    def _row_to_doc(row: dict[str, Any]) -> CoWriterDoc:
        return CoWriterDoc(
            id=str(row["id"]),
            title=str(row["title"]),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )


def _now() -> float:
    return time.time()


# ---- 单例装配（路由取 app.state，其余取单例；照 notebooks 的存取器模式） ----

_co_writer_service: CoWriterService | None = None


def get_co_writer_service() -> CoWriterService:
    if _co_writer_service is None:
        raise RuntimeError("Co-Writer 服务未装配（lifespan 未调用 set_co_writer_service）")
    return _co_writer_service


def set_co_writer_service(service: CoWriterService | None) -> None:
    global _co_writer_service
    _co_writer_service = service
