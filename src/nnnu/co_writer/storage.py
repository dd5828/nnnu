"""Co-Writer 存储（§7.13）：正文文件读写 + 内存态待确认编辑。

- 正文不进 SQLite：一篇文档一个文件 data/user/co_writer/<doc_id>.md（元数据表
  co_writer_docs 见 schema v13）。doc_id 直接拼进路径，先卡形状再谈穿越——非法
  id 一律当不存在。写入复用记忆层的 atomic_write_text（同目录临时文件 + fsync +
  replace，Windows 瞬锁重试）。
- 并发过每文件一把 asyncio.Lock：服务层持锁做「读-校验-写」（自动保存与 accept
  写回会撞），本类的读写是同步 IO（文件小、量少），与 MemoryStore 同一套用法。
- 待确认编辑只放内存（不落库）：进程重启后 accept/reject 一律 409 edit_expired
  （前端有文案说明）。
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nnnu.co_writer.models import (
    PREVIEW_HEAD_CHARS,
    CoWriterError,
    is_valid_doc_id,
    validate_content,
)
from nnnu.services.memory.store import atomic_write_text

PENDING_TTL_SECONDS = 30 * 60  # 半小时不确认就当它过期
PENDING_MAX = 20  # 内存里最多挂多少条待确认编辑


@dataclass(slots=True)
class PendingEdit:
    """一次等确认的改写；digest 是发起时的正文摘要，accept 前比对防覆盖新内容。"""

    edit_id: str
    doc_id: str
    start: int
    end: int
    original: str
    edited: str
    digest: str
    created_at: float = field(default_factory=time.time)


class CoWriterStorage:
    def __init__(self, data_root: Path) -> None:
        self._root = data_root / "user" / "co_writer"
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, doc_id: str) -> Path | None:
        """id 形状不对返回 None（调用方把它当「文档不存在」）。"""
        if not is_valid_doc_id(doc_id):
            return None
        return self._root / f"{doc_id}.md"

    def lock_for(self, doc_id: str) -> asyncio.Lock:
        lock = self._locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[doc_id] = lock
        return lock

    def load(self, doc_id: str) -> str | None:
        """读正文；非法 id → None（当不存在），合法 id 但文件缺失 → 空文档（""）。"""
        path = self.path_for(doc_id)
        if path is None:
            return None
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def head(self, doc_id: str, limit: int = PREVIEW_HEAD_CHARS) -> str:
        """只读前 limit 个字符（列表页算预览用，不整篇读盘）。"""
        path = self.path_for(doc_id)
        if path is None or not path.is_file():
            return ""
        with path.open("r", encoding="utf-8") as handle:
            return handle.read(limit)

    def save(self, doc_id: str, content: str) -> None:
        path = self.path_for(doc_id)
        if path is None:
            raise CoWriterError("文档 id 形状不对", code="invalid_doc_id")
        atomic_write_text(path, validate_content(content))

    def delete(self, doc_id: str) -> None:
        path = self.path_for(doc_id)
        if path is not None:
            path.unlink(missing_ok=True)


class PendingEditStore:
    """待确认编辑的内存货架：超时或塞满就把旧的清掉；clock 可注入（测试用）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = PENDING_TTL_SECONDS,
        max_pending: int = PENDING_MAX,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_pending
        self._clock = clock
        self._items: dict[str, PendingEdit] = {}

    def put(self, edit: PendingEdit) -> None:
        self._prune()
        if len(self._items) >= self._max:
            oldest = min(self._items.values(), key=lambda item: item.created_at)
            self._items.pop(oldest.edit_id, None)
        self._items[edit.edit_id] = edit

    def pop(self, edit_id: str) -> PendingEdit | None:
        """取走（accept/reject 消费一次）；过期或不存在返回 None。"""
        self._prune()
        return self._items.pop(edit_id, None)

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, item in self._items.items() if now - item.created_at > self._ttl]:
            self._items.pop(key, None)
