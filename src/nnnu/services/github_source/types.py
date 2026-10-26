"""GitHub 源的数据模型（§7.9，P14）。

一个知识库可以绑多个源（`sources.json` 与 manifest 同目录，原子写）：

    {
      "sources": [
        {"source_id": "src-1a2b3c4d", "repo": "owner/name", "branch": "main",
         "glob": "docs/**/*.md", "auto_sync": true, "interval_minutes": 60,
         "docs": {"docs/intro.md": {"path": "...", "sha": "...", "doc_id": "kbdoc-..."}}}
      ]
    }

`docs` 是「远端路径 → 上次同步的 blob sha / 对应 KB 文档 id」，漂移检查
（远端 sha 变了没有、文件还在不在）全靠它。这里只存同步记账，文档本体仍在
KB 的 uploads 目录里，manifest 是唯一权威。
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field

from nnnu.services.knowledge import manifest as store
from nnnu.services.settings.atomic import atomic_write_json

SOURCES_FILE = "sources.json"
SOURCE_ID_PREFIX = "src"
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60
DEFAULT_INTERVAL_MINUTES = 60


class SourceDoc(BaseModel):
    """一条已同步文件：远端路径、blob sha、对应 KB 文档 id。"""

    path: str
    sha: str
    doc_id: str


class GithubSource(BaseModel):
    source_id: str
    repo: str  # owner/name
    branch: str = "main"
    glob: str = "**/*.md"
    auto_sync: bool = True
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    last_check_at: float = 0.0
    last_sync_at: float = 0.0
    last_error: str = ""
    docs: dict[str, SourceDoc] = Field(default_factory=dict)

    def due(self, now: float | None = None) -> bool:
        if not self.auto_sync:
            return False
        stamp = max(self.last_check_at, self.last_sync_at)
        if stamp <= 0:
            return True
        return (now if now is not None else time.time()) - stamp >= self.interval_minutes * 60


class SourcesFile(BaseModel):
    sources: list[GithubSource] = Field(default_factory=list)


class SourceSyncReport(BaseModel):
    """一次同步/检查的结果（界面直接展示）。"""

    source_id: str
    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    unchanged: int = 0
    skipped: list[str] = Field(default_factory=list)  # 非文本/超限等
    failed: list[dict[str, str]] = Field(default_factory=list)  # [{"path","error"}]
    docs_created: int = 0
    docs_updated: int = 0
    docs_deleted: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def sources_path(data_root: Path, kb_id: str) -> Path:
    return store.kb_dir(data_root, kb_id) / SOURCES_FILE


def read_sources(data_root: Path, kb_id: str) -> list[GithubSource]:
    path = sources_path(data_root, kb_id)
    if not path.is_file():
        return []
    try:
        raw = SourcesFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return raw.sources


def write_sources(data_root: Path, kb_id: str, sources: list[GithubSource]) -> None:
    path = sources_path(data_root, kb_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, SourcesFile(sources=sources).model_dump())
