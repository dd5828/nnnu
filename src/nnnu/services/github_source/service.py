"""GitHub 源同步（§7.9，P14）：把仓库里的 Markdown/文本文件变成知识库文档。

三个动作分开，界面能各自展示：

- **check**：只拉文件树、比对 blob sha，告诉用户「新增/改动/删除」各有多少；
- **sync**：按差异下载内容、增改删 KB 文档（删除是软删，检索立即排除）；
- **定时**：后台循环里对到期且 auto_sync 的源跑 sync。

几条约定：
- 只收文本类文件（normalize_mime 归一到 text/*）；图片、PDF 等跳过并回报，
  不让一个二进制文件把整次同步弄失败；
- 同一路径的 blob sha 没变就完全不下载（这就是漂移检查省流量的地方）；
- 一次同步里新增/更新多个文件只触发一轮 KB 构建（`kick=False` 批量收尾）；
- 所有异常都记到源的 `last_error`，界面能看到「上次为什么没同步成」。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from nnnu.core.ids import new_id
from nnnu.services.audit import audit_log
from nnnu.services.files.service import normalize_mime
from nnnu.services.github_source import types as store
from nnnu.services.github_source.client import (
    MAX_FILE_BYTES,
    GithubClient,
    GithubConfigError,
    GithubError,
    TreeFile,
    validate_repo,
)
from nnnu.services.github_source.types import (
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    GithubSource,
    SourceDoc,
    SourceSyncReport,
)
from nnnu.services.knowledge.service import KBError, KBService

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], GithubClient]
SYNC_TICK_S = 60.0
STARTUP_DELAY_S = 30.0


class SourceError(ValueError):
    """源配置不合法（422 类错误，消息直接给用户）。"""


class SourceNotFound(SourceError):
    """知识库或源不存在（404）。"""


def compile_glob(pattern: str) -> re.Pattern[str]:
    """gitignore 风格 glob 的子集：`**` 跨目录、`*`/`?` 不跨 `/`。

    为什么不用 fnmatch：fnmatch 的 `*` 会跨 `/`，`docs/*.md` 会误配
    `docs/a/b.md`；转成锚定正则才能表达「这一层文件」。
    """
    clean = (pattern or "").strip()
    if not clean:
        raise SourceError("glob 不能为空，例如 docs/**/*.md")
    out: list[str] = ["^"]
    index = 0
    while index < len(clean):
        if clean.startswith("**/", index):
            out.append("(?:.*/)?")  # `**/` 也匹配零层目录（根目录文件）
            index += 3
        elif clean.startswith("**", index):
            out.append(".*")
            index += 2
        elif clean[index] == "*":
            out.append("[^/]*")
            index += 1
        elif clean[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(clean[index]))
            index += 1
    out.append("$")
    return re.compile("".join(out))


def _is_text_path(path: str) -> bool:
    mime = normalize_mime(path, None)
    return bool(mime and mime.startswith("text/"))


def _validate_interval(value: int) -> int:
    interval = int(value or 0)
    if interval < MIN_INTERVAL_MINUTES or interval > MAX_INTERVAL_MINUTES:
        raise SourceError(f"同步间隔要在 {MIN_INTERVAL_MINUTES}~{MAX_INTERVAL_MINUTES} 分钟之间")
    return interval


class GithubSourceService:
    def __init__(
        self,
        kb: KBService,
        *,
        client_factory: ClientFactory | None = None,
        tick_interval: float = SYNC_TICK_S,
        startup_delay: float = STARTUP_DELAY_S,
    ) -> None:
        self._kb = kb
        self._data_root = kb.data_root
        self._client_factory: ClientFactory = client_factory or GithubClient
        self._tick = tick_interval
        self._startup_delay = startup_delay
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None

    # ================= 增删改查 =================

    def list_sources(self, kb_id: str) -> list[GithubSource]:
        self._require_kb(kb_id)
        return store.read_sources(self._data_root, kb_id)

    def get_source(self, kb_id: str, source_id: str) -> GithubSource:
        for source in self.list_sources(kb_id):
            if source.source_id == source_id:
                return source
        raise SourceNotFound("这个 GitHub 源不存在（可能已被删除）")

    async def add_source(
        self,
        kb_id: str,
        *,
        repo: str,
        branch: str = "main",
        glob: str = "**/*.md",
        auto_sync: bool = True,
        interval_minutes: int = store.DEFAULT_INTERVAL_MINUTES,
    ) -> GithubSource:
        self._require_kb(kb_id)
        try:
            clean_repo = validate_repo(repo)
        except GithubConfigError as exc:
            raise SourceError(str(exc)) from exc
        clean_branch = (branch or "").strip()
        if not clean_branch:
            raise SourceError("分支不能为空")
        compile_glob(glob)
        interval = _validate_interval(interval_minutes)
        sources = store.read_sources(self._data_root, kb_id)
        for item in sources:
            if (item.repo, item.branch, item.glob) == (clean_repo, clean_branch, glob.strip()):
                raise SourceError("同一个仓库、分支和 glob 已经绑定过了")
        source = GithubSource(
            source_id=new_id("src"),
            repo=clean_repo,
            branch=clean_branch,
            glob=glob.strip(),
            auto_sync=auto_sync,
            interval_minutes=interval,
        )
        sources.append(source)
        store.write_sources(self._data_root, kb_id, sources)
        audit_log("github_source_add", kb_id=kb_id, source_id=source.source_id, repo=clean_repo)
        return source

    async def update_source(
        self,
        kb_id: str,
        source_id: str,
        *,
        repo: str | None = None,
        branch: str | None = None,
        glob: str | None = None,
        auto_sync: bool | None = None,
        interval_minutes: int | None = None,
    ) -> GithubSource:
        async with self._lock(kb_id, source_id):
            sources = store.read_sources(self._data_root, kb_id)
            source = self._find(sources, source_id)
            if repo is not None:
                try:
                    source.repo = validate_repo(repo)
                except GithubConfigError as exc:
                    raise SourceError(str(exc)) from exc
            if branch is not None:
                clean_branch = branch.strip()
                if not clean_branch:
                    raise SourceError("分支不能为空")
                source.branch = clean_branch
            if glob is not None:
                compile_glob(glob)
                source.glob = glob.strip()
            if auto_sync is not None:
                source.auto_sync = auto_sync
            if interval_minutes is not None:
                source.interval_minutes = _validate_interval(interval_minutes)
            store.write_sources(self._data_root, kb_id, sources)
            audit_log(
                "github_source_update",
                kb_id=kb_id,
                source_id=source_id,
                repo=source.repo,
                branch=source.branch,
                glob=source.glob,
            )
            return source

    async def delete_source(
        self, kb_id: str, source_id: str, *, remove_docs: bool = True
    ) -> GithubSource:
        async with self._lock(kb_id, source_id):
            sources = store.read_sources(self._data_root, kb_id)
            source = self._find(sources, source_id)
            if remove_docs:
                for item in source.docs.values():
                    try:
                        await self._kb.delete_doc(kb_id, item.doc_id)
                    except KBError:
                        continue
            sources = [item for item in sources if item.source_id != source_id]
            store.write_sources(self._data_root, kb_id, sources)
            audit_log("github_source_delete", kb_id=kb_id, source_id=source_id)
            return source

    # ================= 检查 / 同步 =================

    async def check_source(self, kb_id: str, source_id: str) -> SourceSyncReport:
        lock = self._lock(kb_id, source_id)
        async with lock:
            source = self.get_source(kb_id, source_id)

            async def run(client: GithubClient) -> SourceSyncReport:
                remote, skipped = await self._remote_files(client, source)
                report = self._diff(source, remote)
                report.skipped = skipped
                return report

            return await self._run_remote(kb_id, source, run, mark_check=True)

    async def sync_source(self, kb_id: str, source_id: str) -> SourceSyncReport:
        lock = self._lock(kb_id, source_id)
        async with lock:
            source = self.get_source(kb_id, source_id)

            async def run(client: GithubClient) -> SourceSyncReport:
                return await self._sync_locked(kb_id, source, client)

            return await self._run_remote(kb_id, source, run, mark_sync=True)

    async def check_all(self, kb_id: str) -> list[SourceSyncReport]:
        reports: list[SourceSyncReport] = []
        for source in self.list_sources(kb_id):
            try:
                reports.append(await self.check_source(kb_id, source.source_id))
            except (GithubError, SourceError) as exc:
                logger.warning("检查 GitHub 源 %s 失败：%s", source.source_id, exc)
                reports.append(
                    SourceSyncReport(
                        source_id=source.source_id, failed=[{"path": "", "error": str(exc)}]
                    )
                )
        return reports

    async def sync_all(self, kb_id: str) -> list[SourceSyncReport]:
        reports: list[SourceSyncReport] = []
        for source in self.list_sources(kb_id):
            try:
                reports.append(await self.sync_source(kb_id, source.source_id))
            except (GithubError, SourceError) as exc:
                logger.warning("同步 GitHub 源 %s 失败：%s", source.source_id, exc)
                reports.append(
                    SourceSyncReport(
                        source_id=source.source_id, failed=[{"path": "", "error": str(exc)}]
                    )
                )
        return reports

    async def sync_all_due(self) -> int:
        """后台循环入口：所有到期且 auto_sync 的源各同步一次；返回同步了几个。"""
        count = 0
        now = time.time()
        for manifest in self._kb.list_kbs():
            for source in store.read_sources(self._data_root, manifest.id):
                if not source.due(now):
                    continue
                lock = self._lock(manifest.id, source.source_id)
                if lock.locked():  # 手动同步正在跑，别插队
                    continue
                try:
                    await self.sync_source(manifest.id, source.source_id)
                    count += 1
                except (GithubError, SourceError) as exc:
                    logger.warning("定时同步 GitHub 源 %s 失败：%s", source.source_id, exc)
        return count

    async def _sync_locked(
        self, kb_id: str, source: GithubSource, client: GithubClient
    ) -> SourceSyncReport:
        remote, skipped = await self._remote_files(client, source)
        report = self._diff(source, remote)
        report.skipped = skipped
        changed_docs = False
        for path in [*report.added, *report.modified]:
            tree_file = remote[path]
            try:
                content = await client.download(source.repo, source.branch, path)
                doc_id = await self._upsert_doc(kb_id, source, tree_file, content)
            except (GithubError, KBError) as exc:
                report.failed.append({"path": path, "error": str(exc)})
                continue
            previous = source.docs.get(path)
            if previous is not None and previous.doc_id == doc_id:
                report.docs_updated += 1
            else:
                report.docs_created += 1
            changed_docs = True
            source.docs[path] = SourceDoc(path=path, sha=tree_file.sha, doc_id=doc_id)
        for path in report.deleted:
            stored = source.docs.pop(path, None)
            if stored is None:
                continue
            try:
                if await self._kb.delete_doc(kb_id, stored.doc_id):
                    report.docs_deleted += 1
            except KBError as exc:
                report.failed.append({"path": path, "error": str(exc)})
        if changed_docs:
            self._kb.kick_build(kb_id)
        return report

    async def _upsert_doc(
        self, kb_id: str, source: GithubSource, tree_file: TreeFile, content: bytes
    ) -> str:
        """有旧文档就原地替换，没有就新建；返回 doc_id。"""
        existing = source.docs.get(tree_file.path)
        manifest = self._kb.get_kb(kb_id)
        current = manifest.doc(existing.doc_id) if manifest and existing else None
        if current is not None and current.status != "deleted":
            doc = await self._kb.replace_doc(
                kb_id,
                current.doc_id,
                tree_file.path,
                content,
                None,
                remote_sha=tree_file.sha,
                kick=False,
            )
            return doc.doc_id
        doc = await self._kb.add_doc(
            kb_id,
            tree_file.path,
            content,
            None,
            source_kind="github",
            source_id=source.source_id,
            source_path=tree_file.path,
            remote_sha=tree_file.sha,
            kick=False,
        )
        return doc.doc_id

    async def _remote_files(
        self, client: GithubClient, source: GithubSource
    ) -> tuple[dict[str, TreeFile], list[str]]:
        matcher = compile_glob(source.glob)
        selected: dict[str, TreeFile] = {}
        skipped: list[str] = []
        for item in await client.list_files(source.repo, source.branch):
            if not matcher.match(item.path):
                continue
            if not _is_text_path(item.path):
                skipped.append(item.path)
                continue
            if item.size > MAX_FILE_BYTES:
                skipped.append(item.path)
                continue
            selected[item.path] = item
        return selected, skipped

    @staticmethod
    def _diff(source: GithubSource, remote: dict[str, TreeFile]) -> SourceSyncReport:
        stored = set(source.docs)
        present = set(remote)
        modified = [
            path for path in sorted(stored & present) if source.docs[path].sha != remote[path].sha
        ]
        return SourceSyncReport(
            source_id=source.source_id,
            added=sorted(present - stored),
            modified=modified,
            deleted=sorted(stored - present),
            unchanged=len(stored & present) - len(modified),
        )

    async def _run_remote(
        self,
        kb_id: str,
        source: GithubSource,
        run: Callable[[GithubClient], Awaitable[SourceSyncReport]],
        *,
        mark_check: bool = False,
        mark_sync: bool = False,
    ) -> SourceSyncReport:
        client = self._client_factory()
        try:
            report = await run(client)
        except GithubError as exc:
            self._set_error(kb_id, source.source_id, str(exc))
            raise
        finally:
            await client.aclose()
        now = time.time()
        if mark_check:
            source.last_check_at = now
        if mark_sync:
            source.last_sync_at = now
            source.last_check_at = now
        source.last_error = f"{len(report.failed)} 个文件同步失败" if report.failed else ""
        # 直接保存手里这份（sync 过程中它已经带上新的 docs 映射）；这里再去
        # 读一次盘会把刚同步好的映射覆盖回旧值
        self._save_source(kb_id, source)
        audit_log(
            "github_source_sync" if mark_sync else "github_source_check",
            kb_id=kb_id,
            source_id=source.source_id,
            added=len(report.added),
            modified=len(report.modified),
            deleted=len(report.deleted),
            failed=len(report.failed),
        )
        return report

    # ================= 后台循环 =================

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="github-source-sync")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        if self._startup_delay > 0:
            await asyncio.sleep(self._startup_delay)
        while True:
            try:
                await self.sync_all_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # 定时任务永远不该把进程弄崩
                logger.exception("GitHub 源定时同步失败")
            await asyncio.sleep(self._tick)

    # ================= 小工具 =================

    def _require_kb(self, kb_id: str) -> None:
        if self._kb.get_kb(kb_id) is None:
            raise SourceNotFound(f"知识库 {kb_id} 不存在")

    @staticmethod
    def _find(sources: list[GithubSource], source_id: str) -> GithubSource:
        for source in sources:
            if source.source_id == source_id:
                return source
        raise SourceNotFound("这个 GitHub 源不存在（可能已被删除）")

    def _save_source(self, kb_id: str, source: GithubSource) -> None:
        sources = store.read_sources(self._data_root, kb_id)
        for index, item in enumerate(sources):
            if item.source_id == source.source_id:
                sources[index] = source
                break
        else:
            return  # 同步途中源被删了：别把它写回来
        store.write_sources(self._data_root, kb_id, sources)

    def _set_error(self, kb_id: str, source_id: str, message: str) -> None:
        sources = store.read_sources(self._data_root, kb_id)
        for source in sources:
            if source.source_id == source_id:
                source.last_error = message
                # 失败也算检查过一次：否则定时循环每个 tick 都会重捶 GitHub
                source.last_check_at = time.time()
                break
        else:
            return
        store.write_sources(self._data_root, kb_id, sources)

    def _lock(self, kb_id: str, source_id: str) -> asyncio.Lock:
        key = (kb_id, source_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock
