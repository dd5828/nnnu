"""知识库服务（§7.9）：生命周期、状态机、版本切换、检索门面。

一个 KB 一个后台任务（asyncio.Task + 每 KB 一把 manifest 锁），任务里按文档
串行跑：解析 → 切块 → 嵌入 → 写索引。串行是有意的——嵌入本来就吃满 CPU，
并发只会互相抢；而「构建中还能查旧版本」由版本目录保证，不靠并发。

三条容易被忽略的规矩，都在这里落实：

1. **meta.json 最后写**：版本目录三件套落齐后才写 ready 标志，然后才原子改
   manifest 的 active_version。任何时刻崩溃，指针都指向完整版本（验收 C）。
2. **删除不重写索引**：文档置 deleted，检索时按 doc_id 过滤（引擎的
   excluded_docs）。删一份 200 页 PDF 里的一个坏文档，不该等全量重建（验收 D）。
3. **取消要能恢复原状**：重建期间文档状态会被改成 parsing/embedding；中途取消
   得把「本来在旧索引里、状态是 done」的文档恢复回去，否则它会永远卡在
   embedding 上，而旧索引里其实还有它。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np

from nnnu.core.ids import new_id
from nnnu.services.audit import audit_log
from nnnu.services.embedding.base import BuildCancelled
from nnnu.services.embedding.service import EmbeddingService, get_embedding_service
from nnnu.services.files.service import ALLOWED_MIMES, extension_for, normalize_mime
from nnnu.services.knowledge import manifest as store
from nnnu.services.knowledge.types import (
    DOC_CHUNKING,
    DOC_DELETED,
    DOC_DONE,
    DOC_EMBEDDING,
    DOC_ERROR,
    DOC_IN_FLIGHT,
    DOC_PARSING,
    DOC_REBUILDABLE,
    KB_CREATING,
    KB_ERROR,
    KB_INDEXING,
    KB_READY,
    KbBuild,
    KbDoc,
    KbManifest,
    KbVersion,
)
from nnnu.services.parsing.service import ParsedDocument, parse_document
from nnnu.services.rag.base import BaseEngine, Chunk, Hit
from nnnu.services.rag.chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, chunk_document
from nnnu.services.rag.pipelines.vector import VectorEngine
from nnnu.services.rag.rrf import rrf_fuse

logger = logging.getLogger(__name__)

MAX_KB_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB（§7.9 KB 上传上限，比附件宽松）
# 重建力度：全部重切（换 chunk 参数只能这样）／只重试失败文档（普通的「重建索引」）
REBUILD_ALL = "all"
REBUILD_RETRY = "retry"
# 知识库里图片没有意义（索引不了像素），白名单 = 附件白名单 − image/*
KB_MIME_WHITELIST = frozenset(mime for mime in ALLOWED_MIMES if not mime.startswith("image/"))
PROGRESS_WRITE_INTERVAL_S = 0.2  # 进度写盘节流：轮询 800ms，写太勤没意义


class KBError(RuntimeError):
    """对外的业务错误（名称非法、库不存在、上传超限…）。"""


class KBService:
    def __init__(
        self,
        data_root: Path,
        *,
        engine: BaseEngine | None = None,
        embedder: EmbeddingService | None = None,
    ) -> None:
        self._data_root = Path(data_root)
        self._engine = engine or VectorEngine()
        self._embedder = embedder or get_embedding_service()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._rebuild: dict[str, str] = {}  # kb_id → REBUILD_*（下一次构建生效）

    # ================= 读 =================

    def list_kbs(self) -> list[KbManifest]:
        manifests = [
            store.read_manifest(self._data_root, kb_id)
            for kb_id in store.list_kb_ids(self._data_root)
        ]
        return [item for item in manifests if item is not None]

    def get_kb(self, kb_id: str) -> KbManifest | None:
        try:
            return store.read_manifest(self._data_root, kb_id)
        except store.KbNameError:
            return None

    def has_ready_kb(self) -> bool:
        """挂不挂 rag 工具的判据：至少有一个库真的能搜（ready 且有活跃版本）。"""
        return any(item.status == KB_READY and item.active_version > 0 for item in self.list_kbs())

    def doc_file(self, kb_id: str, doc_id: str) -> Path | None:
        """原始文件路径（阅读器用）；文档不存在或文件没了返回 None。"""
        manifest = self.get_kb(kb_id)
        doc = manifest.doc(doc_id) if manifest else None
        if doc is None or not doc.stored_name:
            return None
        path = store.upload_dir(self._data_root, kb_id, doc_id) / doc.stored_name
        return path if path.is_file() else None

    def doc_parsed(self, kb_id: str, doc_id: str) -> dict[str, Any] | None:
        """解析缓存（文本类阅读器用：前端拿 text 直接渲染）。"""
        path = store.upload_dir(self._data_root, kb_id, doc_id) / "parsed.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        parsed = raw.get("parsed") if isinstance(raw, dict) else None
        return parsed if isinstance(parsed, dict) else None

    # ================= 建/删 =================

    async def create_kb(self, name: str) -> KbManifest:
        clean = store.validate_kb_name(name)
        kb_id = new_id("kb")
        directory = store.kb_dir(self._data_root, kb_id)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = KbManifest(
            id=kb_id,
            name=self._unique_name(clean),
            status=KB_CREATING,
        )
        store.write_manifest(self._data_root, manifest)
        # 空库没有待办：创建是一瞬间的事，直接落到 ready（首次上传才会进 indexing）
        manifest.status = KB_READY
        store.write_manifest(self._data_root, manifest)
        audit_log("kb_create", kb_id=kb_id, name=manifest.name)
        return manifest

    async def delete_kb(self, kb_id: str) -> bool:
        manifest = self.get_kb(kb_id)
        if manifest is None:
            return False
        await self.cancel_build(kb_id)
        await self.wait_idle(kb_id)
        store.remove_tree(store.kb_dir(self._data_root, kb_id))
        uploads = self._data_root / "user" / "uploads" / "kb" / kb_id
        store.remove_tree(uploads)
        self._forget(kb_id)
        audit_log("kb_delete", kb_id=kb_id, name=manifest.name, doc_count=len(manifest.docs))
        return True

    async def add_doc(self, kb_id: str, filename: str, content: bytes, mime: str | None) -> KbDoc:
        """落盘 + 记账 + 触发构建；返回文档记录（status 此刻是 parsing）。"""
        if not content:
            raise KBError("文件是空的")
        if len(content) > MAX_KB_UPLOAD_BYTES:
            raise KBError(f"文件超过 {MAX_KB_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
        normalized = normalize_mime(filename, mime)
        if normalized is None or normalized not in KB_MIME_WHITELIST:
            raise KBError(f"知识库不支持这种文件类型：{filename}")
        manifest = self.get_kb(kb_id)
        if manifest is None:
            raise KBError("知识库不存在")

        doc_id = new_id("kbdoc")
        directory = store.upload_dir(self._data_root, kb_id, doc_id)
        directory.mkdir(parents=True, exist_ok=True)
        stored_name = f"original{extension_for(filename, normalized)}"
        (directory / stored_name).write_bytes(content)

        doc = KbDoc(
            doc_id=doc_id,
            filename=Path(filename).name[:200],
            stored_name=stored_name,
            mime=normalized,
            size=len(content),
            fingerprint=f"sha256:{_sha256(content)}",
            status=DOC_PARSING,
            note="排队等待索引",
        )
        async with self._lock(kb_id):
            manifest = self.get_kb(kb_id)
            if manifest is None:
                raise KBError("知识库不存在")
            manifest.docs.append(doc)
            manifest.status = KB_INDEXING
            manifest.error = None
            store.write_manifest(self._data_root, manifest)
        audit_log("kb_doc_add", kb_id=kb_id, doc_id=doc_id, filename=doc.filename, size=doc.size)
        self._kick(kb_id)
        return doc

    async def delete_doc(self, kb_id: str, doc_id: str) -> bool:
        """单文档删除（含 error 态）：置 deleted 即从检索里消失，不重写索引。"""
        async with self._lock(kb_id):
            manifest = self.get_kb(kb_id)
            doc = manifest.doc(doc_id) if manifest else None
            if manifest is None or doc is None:
                return False
            if doc.status == DOC_DELETED:
                return True
            was_indexed = doc.status == DOC_DONE
            doc.status = DOC_DELETED
            doc.progress = 0.0
            doc.note = "已删除"
            if not was_indexed and manifest.build is not None:
                # 还没进索引：从本次构建的工作表里摘掉，构建完再删目录
                manifest.build.doc_ids = [item for item in manifest.build.doc_ids if item != doc_id]
                manifest.build.previous.pop(doc_id, None)
            store.write_manifest(self._data_root, manifest)
        audit_log("kb_doc_delete", kb_id=kb_id, doc_id=doc_id, filename=doc.filename)
        # 没有构建在跑就直接清目录；有的话交给 worker 收尾（文件可能正被解析）
        if kb_id not in self._running_tasks():
            store.remove_tree(store.upload_dir(self._data_root, kb_id, doc_id))
        return True

    async def reindex(self, kb_id: str, *, force: bool = True) -> KbManifest:
        """重建：force=True 全量重切重嵌（换 chunk 参数后只能这样），
        force=False 只重跑失败/没做完的文档（快，日常用）。"""
        manifest = self.get_kb(kb_id)
        if manifest is None:
            raise KBError("知识库不存在")
        self._rebuild[kb_id] = REBUILD_ALL if force else REBUILD_RETRY
        async with self._lock(kb_id):
            manifest = self.get_kb(kb_id)
            if manifest is None:
                raise KBError("知识库不存在")
            manifest.error = None
            manifest.status = KB_INDEXING
            store.write_manifest(self._data_root, manifest)
        audit_log("kb_reindex", kb_id=kb_id, force=force)
        self._kick(kb_id)
        return manifest

    async def cancel_build(self, kb_id: str) -> bool:
        manifest = self.get_kb(kb_id)
        if manifest is None or manifest.build is None:
            return False
        event = self._cancel.get(kb_id)
        if event is not None:
            event.set()
        async with self._lock(kb_id):
            manifest = self.get_kb(kb_id)
            if manifest is not None and manifest.build is not None:
                manifest.build.cancel_requested = True
                manifest.build.note = "正在取消…"
                store.write_manifest(self._data_root, manifest)
        audit_log("kb_build_cancel", kb_id=kb_id)
        return True

    # ================= 检索 =================

    async def search(
        self, kb_id: str, query: str, *, mode: str = "hybrid", top_k: int = 5
    ) -> list[Hit]:
        """单库检索；库里没有活跃版本就返回空（不报错，调用方按空处理）。"""
        if not query.strip():
            return []
        manifest = self.get_kb(kb_id)
        if manifest is None or manifest.active_version <= 0:
            return []
        index_dir = store.version_dir(self._data_root, kb_id, manifest.active_version)
        if not store.is_version_ready(index_dir):
            return []
        embedder = await self._embedder.provider()
        hits = await self._engine.query(
            index_dir,
            query,
            kb_id=kb_id,
            top_k=max(1, int(top_k)),
            mode=mode,
            excluded_docs=manifest.excluded_doc_ids(),
            embedder=embedder,
        )
        names = {doc.doc_id: doc.filename for doc in manifest.docs}
        for hit in hits:
            hit.metadata.setdefault("kb_name", manifest.name)
            hit.metadata.setdefault("filename", names.get(hit.doc_id, ""))
        return hits

    async def search_all_ready(
        self, query: str, *, mode: str = "hybrid", top_k: int = 5
    ) -> list[Hit]:
        """跨全部 ready 库检索。

        各库的分数尺度不同（向量余弦 / RRF 分），跨库直接比大小没有意义，
        所以每库先各自排好序，再按名次做一次全局 RRF——只吃名次不吃分数。
        """
        targets = [
            item for item in self.list_kbs() if item.status == KB_READY and item.active_version > 0
        ]
        if not targets or not query.strip():
            return []
        per_kb = await asyncio.gather(
            *(self.search(item.id, query, mode=mode, top_k=top_k) for item in targets)
        )
        keyed: dict[str, Hit] = {}
        ranked_lists: list[list[str]] = []
        for hits in per_kb:
            ids: list[str] = []
            for hit in hits:
                key = f"{hit.kb_id}:{hit.metadata.get('chunk_id') or f'{hit.doc_id}#{hit.page}'}"
                keyed[key] = hit
                ids.append(key)
            if ids:
                ranked_lists.append(ids)
        if not ranked_lists:
            return []
        merged: list[Hit] = []
        for key, score in rrf_fuse(ranked_lists)[: max(1, int(top_k))]:
            merged.append(keyed[key].model_copy(update={"score": score}))
        return merged

    # ================= 启动恢复 =================

    async def recover_stale(self) -> None:
        """启动兜底：上次进程留下的半成品构建/版本目录，这里收干净（幂等）。"""
        for kb_id in store.list_kb_ids(self._data_root):
            manifest = self.get_kb(kb_id)
            if manifest is None:
                continue
            changed = False
            if manifest.build is not None:
                _restore_docs(manifest, note="上次构建中断，可点重建继续")
                active_dir = store.version_dir(self._data_root, kb_id, manifest.active_version)
                if manifest.active_version > 0 and store.is_version_ready(active_dir):
                    manifest.status = KB_READY  # 活跃版本完好，库照样能用
                    manifest.error = None
                else:
                    manifest.status = KB_ERROR
                    manifest.error = "上次构建中断（进程重启），可点重建继续"
                manifest.build = None
                changed = True
            removed = store.prune_unready_versions(self._data_root, kb_id)
            if removed:
                logger.info("清理 %s 的半成品版本目录：%s", kb_id, removed)
            for doc in manifest.docs:
                if doc.status == DOC_DELETED:
                    store.remove_tree(store.upload_dir(self._data_root, kb_id, doc.doc_id))
            if changed:
                store.write_manifest(self._data_root, manifest)

    # ================= 构建 =================

    def _kick(self, kb_id: str) -> None:
        task = self._tasks.get(kb_id)
        if task is None or task.done():
            self._tasks[kb_id] = asyncio.create_task(self._worker(kb_id), name=f"kb-build-{kb_id}")

    def _running_tasks(self) -> set[str]:
        return {kb_id for kb_id, task in self._tasks.items() if not task.done()}

    def _forget(self, kb_id: str) -> None:
        self._tasks.pop(kb_id, None)
        self._cancel.pop(kb_id, None)
        self._locks.pop(kb_id, None)
        self._rebuild.pop(kb_id, None)

    async def wait_idle(self, kb_id: str, timeout: float = 60.0) -> None:
        """等这个库的构建任务收工（测试与删除前用）。"""
        task = self._tasks.get(kb_id)
        if task is None or task.done():
            return
        await asyncio.wait_for(asyncio.shield(task), timeout)

    async def _worker(self, kb_id: str) -> None:
        """循环把待办文档做完；构建期间新加的文档会在下一轮被拾起来。"""
        try:
            while True:
                async with self._lock(kb_id):
                    manifest = self.get_kb(kb_id)
                    if manifest is None:
                        return
                    mode = self._rebuild.pop(kb_id, None)
                    pending = self._build_set(manifest, mode)
                    if not pending:
                        self._finalize(manifest)
                        store.write_manifest(self._data_root, manifest)
                        break
                    version = manifest.next_version()
                    manifest.status = KB_INDEXING
                    manifest.error = None
                    manifest.build = KbBuild(
                        version=version,
                        doc_ids=[doc.doc_id for doc in pending],
                        previous={doc.doc_id: doc.status for doc in pending},
                        note="准备中",
                    )
                    store.write_manifest(self._data_root, manifest)
                    reuse = bool(manifest.active_version) and mode != REBUILD_ALL
                done = await self._build_once(
                    kb_id, version, [doc.doc_id for doc in pending], reuse=reuse
                )
                if not done:  # 被取消：停手，等下一次重建/上传再启动
                    break
        except asyncio.CancelledError:  # pragma: no cover - 关闭进程时的正常路径
            raise
        except Exception as exc:  # 构建崩了：库进 error，不把后台任务变成孤儿异常
            logger.exception("知识库 %s 构建失败", kb_id)
            async with self._lock(kb_id):
                manifest = self.get_kb(kb_id)
                if manifest is not None:
                    manifest.status = KB_ERROR
                    manifest.error = f"构建失败：{type(exc).__name__}: {exc}"
                    manifest.build = None
                    store.write_manifest(self._data_root, manifest)
        finally:
            self._cleanup_deleted_docs(kb_id)

    def _build_set(self, manifest: KbManifest, mode: str | None) -> list[KbDoc]:
        """本次要处理的文档。

        常规（mode=None）只拾「构建途中」的文档——新上传的、以及上一轮没做完的。
        失败态**不**自动重试：否则解析不了的文档会让 worker 一轮一轮空转。
        重试要显式来：整库重切是 REBUILD_ALL，只重试失败文档是 REBUILD_RETRY。
        """
        if mode == REBUILD_ALL:
            return manifest.live_docs()
        if mode == REBUILD_RETRY:
            return [doc for doc in manifest.docs if doc.status in DOC_REBUILDABLE]
        return [doc for doc in manifest.docs if doc.status in DOC_IN_FLIGHT]

    def _finalize(self, manifest: KbManifest) -> None:
        manifest.build = None
        if manifest.active_version > 0:
            manifest.status = KB_READY
            manifest.error = None
        elif manifest.live_docs():
            manifest.status = KB_ERROR
            manifest.error = manifest.error or "索引没有建立成功（文档都解析失败了？）"
        else:
            manifest.status = KB_READY  # 空库：没有待办，就是就绪
            manifest.error = None

    async def _build_once(
        self, kb_id: str, version: int, doc_ids: list[str], *, reuse: bool
    ) -> bool:
        """构建一个版本；返回 False 表示被取消（半成品已丢弃、状态已恢复）。"""
        index_dir = store.version_dir(self._data_root, kb_id, version)
        index_dir.mkdir(parents=True, exist_ok=True)
        cancel = self._cancel.setdefault(kb_id, Event())
        cancel.clear()
        chunk_size, overlap = self._chunk_params()

        chunks: list[Chunk] = []
        blocks: list[np.ndarray] = []
        dim = 0
        if reuse:
            old = self._reuse_base(kb_id, doc_ids)
            if old is not None:
                chunks, blocks, dim = old

        try:
            for doc_id in doc_ids:
                if cancel.is_set():
                    break
                manifest = self.get_kb(kb_id)
                doc = manifest.doc(doc_id) if manifest else None
                if doc is None or doc.status == DOC_DELETED:
                    continue  # 构建途中被删掉：不进新版本
                made = await self._process_doc(kb_id, doc, chunk_size=chunk_size, overlap=overlap)
                if made is None:
                    continue
                doc_chunks, doc_vectors = made
                chunks.extend(doc_chunks)
                blocks.append(doc_vectors)
                dim = doc_vectors.shape[1] if doc_vectors.size else dim
        except BuildCancelled:
            self._abort_build(kb_id, version)
            return False

        if cancel.is_set():
            self._abort_build(kb_id, version)
            return False

        matrix = np.vstack(blocks) if blocks else np.zeros((0, dim), dtype=np.float32)
        if len(chunks) != matrix.shape[0]:
            raise KBError(f"块数 {len(chunks)} 与向量行数 {matrix.shape[0]} 不一致，拒绝写索引")
        await self._engine.write_index(index_dir, chunks=chunks, vectors=matrix, dim=dim)
        # 先落 ready 标志，再切指针——崩溃窗口里指针永远指向完整版本（验收 C）
        store.write_ready_meta(
            index_dir,
            {
                "version": version,
                "dim": dim,
                "chunk_count": len(chunks),
                "doc_count": len({chunk.doc_id for chunk in chunks}),
                "embedding_signature": self._embedder.signature(),
                "created_at": time.time(),
            },
        )
        async with self._lock(kb_id):
            manifest = self.get_kb(kb_id)
            if manifest is None:  # 构建途中库被删了：没什么可切的了
                return False
            manifest.versions = [item for item in manifest.versions if item.version != version]
            manifest.versions.append(
                KbVersion(
                    version=version,
                    doc_count=len({chunk.doc_id for chunk in chunks}),
                    chunk_count=len(chunks),
                    dim=dim,
                    embedding_signature=self._embedder.signature(),
                )
            )
            manifest.active_version = version
            manifest.build = None
            manifest.error = None
            store.write_manifest(self._data_root, manifest)
        logger.info("知识库 %s 索引版本 %s 就绪（%s 块）", kb_id, version, len(chunks))
        return True

    def _reuse_base(
        self, kb_id: str, doc_ids: list[str]
    ) -> tuple[list[Chunk], list[np.ndarray], int] | None:
        """复用旧版本里「不在本次构建名单、且状态是 done」的块与向量。

        增量新增文档时，没必要把整库重新嵌入一遍；只有强制重建（换 chunk
        参数）才全量重来。

        「取回旧版本里的块」是 vector 引擎的能力（load 不在 BaseEngine 契约里：
        graphrag 之类的引擎结构完全不同，硬塞进契约只会让 P14 变形）。换引擎
        就是不增量——退化成全量重建，不是错。
        """
        if not isinstance(self._engine, VectorEngine):
            return None
        manifest = self.get_kb(kb_id)
        if manifest is None or manifest.active_version <= 0:
            return None
        old_dir = store.version_dir(self._data_root, kb_id, manifest.active_version)
        if not store.is_version_ready(old_dir):
            return None
        fresh = set(doc_ids)
        keep = {
            doc.doc_id
            for doc in manifest.docs
            if doc.status == DOC_DONE and doc.doc_id not in fresh
        }
        if not keep:
            return [], [], 0
        loaded = self._engine.load(old_dir)
        rows = [index for index, chunk in enumerate(loaded.chunks) if chunk.doc_id in keep]
        if not rows:
            return [], [], 0
        vectors = loaded.vectors[rows]
        return [loaded.chunks[index] for index in rows], [vectors], int(vectors.shape[1])

    async def _process_doc(
        self, kb_id: str, doc: KbDoc, *, chunk_size: int, overlap: int
    ) -> tuple[list[Chunk], np.ndarray] | None:
        """解析 → 切块 → 嵌入；失败把文档标 error 并返回 None（不拖垮整库）。"""
        if not self._update_doc(
            kb_id,
            doc.doc_id,
            status=DOC_PARSING,
            progress=0.0,
            note="解析中",
            error=None,
        ):
            return None  # 已经删了（或库没了），别再往下做
        try:
            parsed = await self._parse(kb_id, doc)
        except Exception as exc:  # 解析器抛异常也不该让整库失败
            self._update_doc(kb_id, doc.doc_id, status=DOC_ERROR, error=f"解析失败：{exc}", note="")
            return None
        if not parsed.ok:
            self._update_doc(
                kb_id,
                doc.doc_id,
                status=DOC_ERROR,
                error=f"解析失败：{parsed.error or '未知原因'}",
                note="",
            )
            return None

        self._update_doc(kb_id, doc.doc_id, status=DOC_CHUNKING, note="切块中")
        chunks = chunk_document(parsed, doc.doc_id, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            self._update_doc(
                kb_id,
                doc.doc_id,
                status=DOC_ERROR,
                error="文档里没有可索引的文字（扫描件？）",
                note="",
                page_count=parsed.page_count,
            )
            return None

        self._update_doc(
            kb_id,
            doc.doc_id,
            status=DOC_EMBEDDING,
            progress=0.0,
            note=f"嵌入中（0/{len(chunks)} 块）",
            page_count=parsed.page_count,
        )
        throttle = _Throttle(PROGRESS_WRITE_INTERVAL_S)

        def on_note(note: str) -> None:
            self._update_doc(kb_id, doc.doc_id, note=note)

        def on_progress(done: int, total: int) -> None:
            if throttle.ready():
                self._update_doc(
                    kb_id,
                    doc.doc_id,
                    progress=done / max(total, 1),
                    note=f"嵌入中（{done}/{total} 块）",
                )

        try:
            raw = await self._embedder.embed_batch(
                [chunk.text for chunk in chunks],
                on_progress=on_progress,
                on_note=on_note,
                cancel=self._cancel.get(kb_id),
            )
        except BuildCancelled:
            raise
        except Exception as exc:
            self._update_doc(
                kb_id, doc.doc_id, status=DOC_ERROR, error=f"嵌入失败：{exc}", note="", progress=0.0
            )
            return None

        vectors = np.asarray(raw, dtype=np.float32)
        # 嵌入是长活儿，这期间文档可能被删了——那就别把它的块塞进新版本
        if not self._update_doc(
            kb_id,
            doc.doc_id,
            status=DOC_DONE,
            progress=1.0,
            note="",
            error=None,
            chunk_count=len(chunks),
            page_count=parsed.page_count,
        ):
            return None
        return chunks, vectors

    async def _parse(self, kb_id: str, doc: KbDoc) -> ParsedDocument:
        """解析（带缓存：指纹没变就直接读 parsed.json，不重解析）。"""
        directory = store.upload_dir(self._data_root, kb_id, doc.doc_id)
        cached = directory / "parsed.json"
        if cached.is_file():
            try:
                raw = json.loads(cached.read_text(encoding="utf-8"))
                if raw.get("fingerprint") == doc.fingerprint:
                    return ParsedDocument.model_validate(raw["parsed"])
            except (json.JSONDecodeError, OSError, ValueError):
                logger.warning("解析缓存坏了，重新解析：%s", cached)
        original = directory / (doc.stored_name or "original.bin")
        parsed = await asyncio.to_thread(parse_document, original, doc.mime)
        if parsed.ok:
            cached.write_text(
                json.dumps(
                    {"fingerprint": doc.fingerprint, "parsed": parsed.model_dump()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return parsed

    def _abort_build(self, kb_id: str, version: int) -> None:
        """取消：丢掉半成品版本目录，把文档恢复成构建前的状态。"""
        store.remove_version(self._data_root, kb_id, version)
        manifest = self.get_kb(kb_id)
        if manifest is None:
            return
        _restore_docs(manifest, note="已取消，等待重新索引")
        manifest.build = None
        manifest.status = KB_READY if manifest.active_version > 0 else KB_ERROR
        manifest.error = None if manifest.active_version > 0 else "构建已取消"
        store.write_manifest(self._data_root, manifest)
        logger.info("知识库 %s 的构建已取消（丢掉版本 %s）", kb_id, version)

    def _cleanup_deleted_docs(self, kb_id: str) -> None:
        manifest = self.get_kb(kb_id)
        if manifest is None:
            return
        for doc in manifest.docs:
            if doc.status == DOC_DELETED:
                store.remove_tree(store.upload_dir(self._data_root, kb_id, doc.doc_id))

    # ---- 小工具 ----

    def _lock(self, kb_id: str) -> asyncio.Lock:
        lock = self._locks.get(kb_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[kb_id] = lock
        return lock

    def _update_doc(self, kb_id: str, doc_id: str, **fields: Any) -> bool:
        """改一个文档的字段并写盘（构建过程中唯一的进度出口）。

        返回 False = 这次更新没落地（库或文档没了、文档已删除）。deleted 是终态：
        构建任务手里那份文档对象是旧的，它不能把已删除的文档改回 done——否则
        文档复活了，而索引里根本没它的块。
        """
        manifest = self.get_kb(kb_id)
        if manifest is None:
            return False
        doc = manifest.doc(doc_id)
        if doc is None or doc.status == DOC_DELETED:
            return False
        for key, value in fields.items():
            setattr(doc, key, value)
        doc.updated_at = time.time()
        build = manifest.build
        if build is not None and build.doc_ids:
            # 整体进度按「工作表里已 done 的文档数」算：单文档内的嵌入进度在 doc.progress 里
            planned = set(build.doc_ids)
            finished = sum(
                1 for item in manifest.docs if item.doc_id in planned and item.status == DOC_DONE
            )
            build.progress = finished / len(build.doc_ids)
            build.note = doc.note or build.note
            build.stage = doc.status
        store.write_manifest(self._data_root, manifest)
        return True

    def _chunk_params(self) -> tuple[int, int]:
        from nnnu.services.settings.service import get_settings_service

        try:
            values = get_settings_service().load_area("kb")
            size = int(values.get("chunk_size") or DEFAULT_CHUNK_SIZE)
            overlap = int(values.get("chunk_overlap") or DEFAULT_CHUNK_OVERLAP)
        except (KeyError, TypeError, ValueError):
            return DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
        return size, overlap

    def _unique_name(self, name: str) -> str:
        """重名自动加序号（§7.9 允许同名库，id 才是身份）。"""
        existing = {item.name for item in self.list_kbs()}
        if name not in existing:
            return name
        for index in range(2, 1000):
            candidate = f"{name}-{index}"
            if candidate not in existing:
                return candidate
        return name


def _restore_docs(manifest: KbManifest, *, note: str) -> None:
    """构建被取消/中断后，把文档状态恢复成构建前的样子。

    照 KbBuild.previous 恢复，而不是「构建前是不是 done」的二分法：

    - 构建前就 done 的文档在旧索引里好好待着 → 恢复成 done（否则它会永远停在
      embedding 上，而查询其实还能命中它）；
    - 构建前就 error 的 → 恢复成 error，错误文案留着；
    - 构建前就在途的、以及构建期间新加的 → 它们没进过任何索引，记 error + note。
    """
    previous = manifest.build.previous if manifest.build else {}
    for doc in manifest.docs:
        if doc.status == DOC_DELETED:
            continue
        was = previous.get(doc.doc_id)
        if was is None and doc.status not in DOC_IN_FLIGHT:
            continue  # 这次构建没动过它
        doc.progress = 0.0
        if was == DOC_DONE:
            doc.status = DOC_DONE
            doc.progress = 1.0
            doc.error = None
            doc.note = ""
        else:
            doc.status = DOC_ERROR
            doc.note = "" if was == DOC_ERROR else note


class _Throttle:
    """进度写盘节流：至少间隔 interval 秒才放行一次。"""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


_kb_service: KBService | None = None


def get_kb_service() -> KBService:
    if _kb_service is None:
        raise RuntimeError("知识库服务未装配（lifespan 未调用 set_kb_service）")
    return _kb_service


def set_kb_service(service: KBService | None) -> None:
    global _kb_service
    _kb_service = service


def reset_kb_service() -> None:
    """测试用：解除装配。"""
    global _kb_service
    _kb_service = None
