"""附件存储（§7.1）：上传/下载/列表/删除，按会话归档 + 解析缓存。

磁盘布局（chat 能力与 attachment_search 工具按此契约读取，不经 DB）：
  data/user/uploads/<session_id>/<att_id>/
    original.<ext>   原始文件
    parsed.json      解析缓存 {kind, text, pages, page_count, ok, error}

DB 记录（schema v4 attachments 表）用于列表/下载/删除定位。
MIME 白名单 + 扩展名回退（§11.3）；大小上限 P2 常量（设置项 P2 后期入 chat 卡片）。
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nnnu.core.ids import new_id
from nnnu.services.parsing.service import parse_document
from nnnu.services.sessions.db import Database

# §7.1 附件清单：PDF/Office/文本/Markdown/CSV/图片 + 代码扩展名回退
ALLOWED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/markdown",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".go",
    ".rs",
    ".sh",
    ".sql",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".ipynb",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB（§7.19 chat 卡片设置项，P2 先常量）
MAX_ATTACHMENTS_PER_TURN = 5  # §7.1 数量上限（前端 + 后端双检）


class AttachmentRecord(BaseModel):
    id: str
    session_id: str
    name: str
    mime: str
    size: int = 0
    path: str = ""  # data 根相对路径（uploads/<session>/<id>/original.<ext>）
    kind: str = "text"  # text | image
    created_at: float = Field(default_factory=time.time)


def normalize_mime(name: str, mime: str | None) -> str | None:
    """MIME 归一：白名单直通；无 MIME/octet-stream 时按扩展名回退（§11.3 fail-closed）。"""
    if mime and mime in ALLOWED_MIMES:
        return mime
    suffix = Path(name).suffix.lower()
    if suffix in CODE_EXTENSIONS:
        return "text/plain"  # 代码类统一按文本直读
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".csv":
        return "text/csv"
    return None


def extension_for(name: str, mime: str) -> str:
    """归档文件名后缀：优先原扩展名（安全校验过），否则按 MIME 给默认。"""
    suffix = Path(name).suffix.lower()
    if suffix and len(suffix) <= 10 and suffix.isascii():
        return suffix
    return {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime, ".bin")


class AttachmentsService:
    def __init__(self, db: Database, data_root: Path) -> None:
        self._db = db
        self._data_root = data_root
        self._uploads_root = data_root / "user" / "uploads"

    def uploads_root(self) -> Path:
        return self._uploads_root

    def resolve_path(self, record: AttachmentRecord) -> Path:
        """record.path 相对 data 根（如 user/uploads/<sess>/<att>/original.pdf）→ 绝对路径。"""
        return self._data_root / record.path

    async def save_upload(
        self, session_id: str, name: str, content: bytes, mime: str
    ) -> AttachmentRecord:
        """落盘 + 解析缓存 + DB 记录；解析失败不拒绝上传（缓存标 error）。

        解析在线程池执行（markitdown/PDF 可能耗时数秒，不阻塞事件循环）。
        """
        attachment_id = new_id("att")
        directory = self._uploads_root / session_id / attachment_id
        directory.mkdir(parents=True, exist_ok=True)
        original = directory / f"original{extension_for(name, mime)}"
        original.write_bytes(content)

        parsed = await asyncio.to_thread(parse_document, original, mime)
        kind = "image" if mime.startswith("image/") else "text"
        (directory / "parsed.json").write_text(
            json.dumps(parsed.model_dump(), ensure_ascii=False), encoding="utf-8"
        )

        record = AttachmentRecord(
            id=attachment_id,
            session_id=session_id,
            name=name,
            mime=mime,
            size=len(content),
            path=f"user/uploads/{session_id}/{attachment_id}/{original.name}",
            kind=kind,
        )
        await self._db.execute(
            """INSERT INTO attachments (id, session_id, name, mime, size, path, kind, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id,
                record.session_id,
                record.name,
                record.mime,
                record.size,
                record.path,
                record.kind,
                record.created_at,
            ),
        )
        return record

    async def get(self, attachment_id: str) -> AttachmentRecord | None:
        row = await self._db.fetch_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
        return self._row_to_record(row) if row else None

    async def list_for_session(self, session_id: str) -> list[AttachmentRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM attachments WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        )
        return [self._row_to_record(row) for row in rows]

    async def delete(self, attachment_id: str) -> bool:
        record = await self.get(attachment_id)
        if record is None:
            return False
        await self._db.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        # 删除整个归档目录（original + parsed.json），即 §7.1 解析缓存清理入口
        directory = self._uploads_root / record.session_id / record.id
        if directory.is_dir():
            for file in directory.iterdir():
                file.unlink(missing_ok=True)
            directory.rmdir()
        return True

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> AttachmentRecord:
        return AttachmentRecord(
            id=row["id"],
            session_id=row["session_id"],
            name=row["name"],
            mime=row["mime"],
            size=row["size"],
            path=row["path"],
            kind=row["kind"],
            created_at=row["created_at"],
        )
