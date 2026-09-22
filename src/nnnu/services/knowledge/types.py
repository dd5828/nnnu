"""KB 数据模型（§7.9 / §8.3）：manifest 是知识库的唯一权威。

两级状态机（§7.9）：
- KB：creating → indexing → ready | error
- 文档：parsing → chunking → embedding → done | error（deleted 是终态，不参与检索）

文档的状态说的是「它现在打没打进**当前活跃版本**的索引」，所以重建期间文档会
重新走一遍 parsing→…→done，中途取消要把它恢复成构建前的状态（否则它会永远
停在 embedding 上，而旧索引里其实还有它）。
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

KB_CREATING = "creating"
KB_INDEXING = "indexing"
KB_READY = "ready"
KB_ERROR = "error"

DOC_PARSING = "parsing"
DOC_CHUNKING = "chunking"
DOC_EMBEDDING = "embedding"
DOC_DONE = "done"
DOC_ERROR = "error"
DOC_DELETED = "deleted"

# 构建途中的文档（worker 的常规工作表：新上传的文档也在这个状态里等着被拾起）
DOC_IN_FLIGHT = (DOC_PARSING, DOC_CHUNKING, DOC_EMBEDDING)
# 显式重建时额外重试的：解析失败的文档在 error 上等一次重试机会，
# 失败就还是 error，不阻塞整库 ready（§7.9 验收 D）
DOC_REBUILDABLE = (*DOC_IN_FLIGHT, DOC_ERROR)


class KbVersion(BaseModel):
    """一个索引版本（index/version-N 目录）。ready 标志在目录里的 meta.json。"""

    version: int
    doc_count: int = 0
    chunk_count: int = 0
    dim: int = 0
    embedding_signature: str = ""
    created_at: float = Field(default_factory=time.time)


class KbDoc(BaseModel):
    doc_id: str
    filename: str
    stored_name: str = ""  # 落盘文件名（original.pdf 之类），原名只存这里
    mime: str = ""
    size: int = 0
    fingerprint: str = ""  # "sha256:<hex>"（源变化检测，未来 Book 漂移复用）
    status: str = DOC_PARSING
    page_count: int = 0
    chunk_count: int = 0
    error: str | None = None
    progress: float = 0.0  # 0~1，当前阶段的进度
    note: str = ""  # 给人看的一句（"正在下载嵌入模型 42/95 MB"）
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class KbBuild(BaseModel):
    """正在进行的构建（写进 manifest：重启后据此收尾或报错）。"""

    version: int
    doc_ids: list[str] = Field(default_factory=list)
    # 构建前各文档的状态：取消/崩溃后照它恢复（否则文档会永远停在 embedding，
    # 而旧索引里其实还有它）
    previous: dict[str, str] = Field(default_factory=dict)
    stage: str = ""
    progress: float = 0.0
    note: str = ""
    cancel_requested: bool = False
    started_at: float = Field(default_factory=time.time)


class KbManifest(BaseModel):
    id: str
    name: str
    engine: str = "vector"
    status: str = KB_CREATING
    active_version: int = 0  # 0 = 还没有可用版本
    versions: list[KbVersion] = Field(default_factory=list)
    docs: list[KbDoc] = Field(default_factory=list)
    build: KbBuild | None = None
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # ---- 小工具（纯查询，不改状态） ----

    def doc(self, doc_id: str) -> KbDoc | None:
        for item in self.docs:
            if item.doc_id == doc_id:
                return item
        return None

    def live_docs(self) -> list[KbDoc]:
        """参与检索的文档（deleted 之外）。"""
        return [item for item in self.docs if item.status != DOC_DELETED]

    def excluded_doc_ids(self) -> frozenset[str]:
        """已删除文档：查询时按 id 过滤（不重写索引矩阵，删除即时生效）。"""
        return frozenset(item.doc_id for item in self.docs if item.status == DOC_DELETED)

    def next_version(self) -> int:
        return max((item.version for item in self.versions), default=0) + 1
