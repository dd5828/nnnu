"""笔记本服务（§7.15 极简版）：笔记本 CRUD + 记录增删。

批一只做「存得下、看得见」：记录一经写入不可改（要改就删了重存），
移动/复制/导出、`@笔记本:记录` 引用、`write_note` 工具归 P9 §7.15 正式版。
"""

import unicodedata
from typing import Any, cast

from nnnu.services.notebooks.models import Notebook, NotebookRecord, RecordType
from nnnu.services.sessions.db import Database

MAX_NAME_CHARS = 60  # 与知识库名同一个量级：够长，又不至于把卡片撑破
MAX_TITLE_CHARS = 120
MAX_CONTENT_CHARS = 200_000  # 一份解答/一次问答的正文上限（防手滑贴进整本书）

# 记录类型白名单：请求里的 type 落库前必须在这里面
RECORD_TYPES: tuple[str, ...] = ("chat", "solve", "note", "question")

# 取正文首行当标题时要剥掉的 markdown 装饰
_TITLE_STRIP_CHARS = "#*-— \t"


class NotebookError(ValueError):
    """笔记本相关参数非法（空名、超长、未知记录类型）。"""


def validate_name(name: str) -> str:
    """笔记本名校验并归一（NFC）：只管显示名（id 一律 nb-xxxx，不参与）。"""
    normalized = unicodedata.normalize("NFC", (name or "").strip())
    if not normalized:
        raise NotebookError("笔记本名不能为空")
    if len(normalized) > MAX_NAME_CHARS:
        raise NotebookError(f"笔记本名最多 {MAX_NAME_CHARS} 个字符")
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise NotebookError("笔记本名不能包含控制字符")
    return normalized


def _title_of(raw: str | None, content: str) -> str:
    """记录标题：显式给了就用，没给就取正文首个非空行（剥掉标题符号）。"""
    text = (raw or "").strip().strip(_TITLE_STRIP_CHARS)
    if not text:
        for line in content.splitlines():
            stripped = line.strip().strip(_TITLE_STRIP_CHARS)
            if stripped:
                text = stripped
                break
    return (text or "未命名记录")[:MAX_TITLE_CHARS]


class NotebookService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 笔记本 ----

    async def list_notebooks(self) -> list[Notebook]:
        rows = await self._db.fetch_all(
            "SELECT * FROM notebooks ORDER BY created_at DESC, rowid DESC"
        )
        return [self._row_to_notebook(row) for row in rows]

    async def get_notebook(self, notebook_id: str) -> Notebook | None:
        row = await self._db.fetch_one("SELECT * FROM notebooks WHERE id = ?", (notebook_id,))
        return self._row_to_notebook(row) if row else None

    async def create_notebook(self, name: str, description: str | None = None) -> Notebook:
        notebook = Notebook.new(
            name=validate_name(name), description=(description or "").strip() or None
        )
        await self._db.execute(
            "INSERT INTO notebooks (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (notebook.id, notebook.name, notebook.description, notebook.created_at),
        )
        return notebook

    async def delete_notebook(self, notebook_id: str) -> bool:
        """删笔记本：记录靠 ON DELETE CASCADE 一起走（连接已开 foreign_keys=ON）。"""
        cursor = await self._db.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        return cursor.rowcount > 0

    # ---- 记录 ----

    async def list_records(self, notebook_id: str) -> list[NotebookRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM notebook_records WHERE notebook_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (notebook_id,),
        )
        return [self._row_to_record(row) for row in rows]

    async def add_record(
        self,
        notebook_id: str,
        *,
        record_type: str,
        content_md: str,
        title: str = "",
        source_ref: str | None = None,
    ) -> NotebookRecord:
        if record_type not in RECORD_TYPES:
            raise NotebookError(f"未知记录类型 {record_type!r}，允许：{'、'.join(RECORD_TYPES)}")
        content = content_md or ""
        if not content.strip():
            raise NotebookError("记录内容不能为空")
        if len(content) > MAX_CONTENT_CHARS:
            raise NotebookError(f"记录内容最多 {MAX_CONTENT_CHARS} 个字符")
        record = NotebookRecord.new(
            notebook_id=notebook_id,
            type=cast(RecordType, record_type),  # 上面已按 RECORD_TYPES 白名单校验
            title=_title_of(title, content),
            content_md=content,
            source_ref=source_ref,
        )
        await self._db.execute(
            "INSERT INTO notebook_records "
            "(id, notebook_id, type, title, content_md, source_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.notebook_id,
                record.type,
                record.title,
                record.content_md,
                record.source_ref,
                record.created_at,
            ),
        )
        return record

    async def delete_record(self, notebook_id: str, record_id: str) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM notebook_records WHERE id = ? AND notebook_id = ?",
            (record_id, notebook_id),
        )
        return cursor.rowcount > 0

    # ---- 行转换 ----

    @staticmethod
    def _row_to_notebook(row: dict[str, Any]) -> Notebook:
        return Notebook(
            id=str(row["id"]),
            name=str(row["name"]),
            description=row["description"],
            created_at=float(row["created_at"] or 0.0),
        )

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> NotebookRecord:
        return NotebookRecord(
            id=str(row["id"]),
            notebook_id=str(row["notebook_id"]),
            type=row["type"],
            title=str(row["title"]),
            content_md=str(row["content_md"]),
            source_ref=row["source_ref"],
            created_at=float(row["created_at"] or 0.0),
        )
