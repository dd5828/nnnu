"""L2/L3 Markdown 存储（§7.10）：条目解析/渲染 + 原子写 + 每文件锁。

条目格式（自研；id 稳定，删除/插入不漂——§8.3「序号」的防漂移细化）：

    - [2026-09-18] 正文 [L1:chat/2026-09.jsonl#123] [mem-1a2b3c4d]
    - [STALE] [2026-09-19] 正文 [L2:chat#mem-1a2b3c4d, deep_solve#mem-7c8d9e0f] [mem-2b3c4d5e]

- 一条 entry 一行：`- ` + 可选 `[STALE] ` + `[日期] ` + 正文 + 引用方括号 + 可选 `[mem-id]`；
- L1 引用各自成括（引用一行一个证据），L2 引用同面逗号并进一个括（L3 条目常同时
  引多面，并括更紧凑）；id 放最后；
- 解析不出的非空行 = 手写保底（extras），改写时原样带末尾——机器重排绝不丢内容。

**每文件一把 asyncio.Lock（三路共用）**：consolidator 读改写 / write_memory 工具追加 /
PATCH·DELETE API——否则「整合读到一半 + 工具追加」会丢条目（lost update）。
锁只包住「读-改-写」，LLM 调用一律在锁外。
"""

import asyncio
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from nnnu.core.ids import new_id
from nnnu.services.memory import paths
from nnnu.services.memory.models import MemoryDoc, MemoryEntry, normalize_text

# 一条 entry 一行；正文非贪婪，尾部引用括与 id 括从行尾向里吃
ENTRY_RE = re.compile(
    r"^- (?P<stale>\[STALE\] )?\[(?P<date>\d{4}-\d{2}-\d{2})\] (?P<body>.+?)"
    r"(?P<refs>(?: \[(?:L1|L2):[^\]]+\])*)"
    r"(?: \[(?P<eid>mem-[0-9a-f]{8})\])?$"
)

# 从引用段里抠出每个方括号的内容（无方括号）
REF_RE = re.compile(r"\[((?:L1|L2):[^\]]+)\]")

_L2_REF_MEMBER_RE = re.compile(r"^[a-z0-9_]+#mem-[0-9a-f]{8}$")


def default_header(layer: str, key: str) -> str:
    if layer == "l2":
        return f"# L2 · {key} 分面事实"
    return f"# L3 · {paths.L3_TITLES.get(key, key)}"


def _split_refs(raw: str) -> list[str]:
    """引用段 → 规范化引用串列表（`L1:…` / `L2:surface#mem-…`，无方括号）。"""
    refs: list[str] = []
    for match in REF_RE.finditer(raw):
        content = match.group(1)
        if content.startswith("L1:"):
            refs.append(content)
        else:  # L2：同面逗号并括，拆开各自还原前缀
            for member in content[len("L2:") :].split(","):
                member = member.strip()
                if _L2_REF_MEMBER_RE.fullmatch(member):
                    refs.append(f"L2:{member}")
    return refs


def parse_entry(line: str, *, layer: str, key: str, line_no: int = 0) -> MemoryEntry | None:
    """解析单行；不像 entry 返回 None（交调用方当归为头/保底行）。"""
    match = ENTRY_RE.match(line)
    if match is None:
        return None
    body = match.group("body").strip()
    if not body:
        return None
    return MemoryEntry(
        id=match.group("eid"),
        date=match.group("date"),
        text=body,
        refs=_split_refs(match.group("refs") or ""),
        stale=match.group("stale") is not None,
        layer=layer,
        key=key,
        line_no=line_no,
    )


def render_entry(entry: MemoryEntry) -> str:
    """条目 → 一行（与 parse_entry 互逆）。"""
    parts: list[str] = ["- "]
    if entry.stale:
        parts.append("[STALE] ")
    parts.append(f"[{entry.date}] {entry.text}")
    for ref in entry.refs:
        if ref.startswith("L1:"):
            parts.append(f" [{ref}]")
    l2_refs = [ref for ref in entry.refs if ref.startswith("L2:")]
    if l2_refs:
        joined = ", ".join(ref[len("L2:") :] for ref in l2_refs)
        parts.append(f" [L2:{joined}]")
    if entry.id:
        parts.append(f" [{entry.id}]")
    return "".join(parts)


def parse_doc(text: str, *, layer: str, key: str) -> MemoryDoc:
    """整文档解析：首条 entry 之前 → header；之后不匹配的非空行 → extras 保底。"""
    doc = MemoryDoc(layer=layer, key=key)
    header_lines: list[str] = []
    seen_entry = False
    for index, raw in enumerate(text.splitlines(), start=1):
        entry = parse_entry(raw, layer=layer, key=key, line_no=index)
        if entry is not None:
            seen_entry = True
            doc.entries.append(entry)
        elif not seen_entry:
            header_lines.append(raw)
        elif raw.strip():
            doc.extras.append(raw)
    header = "\n".join(header_lines).strip("\n")
    doc.header = header if header.strip() else default_header(layer, key)
    return doc


def render_doc(doc: MemoryDoc) -> str:
    """文档 → 文本（与 parse_doc 往返一致；空文档也带默认头）。"""
    sections: list[str] = [doc.header]
    if doc.entries:
        sections.append("\n".join(render_entry(entry) for entry in doc.entries))
    if doc.extras:
        sections.append("\n".join(doc.extras))
    return "\n\n".join(sections).rstrip() + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：同目录临时文件 + fsync + replace；Windows 瞬锁重试。

    （手法照 services/settings/atomic.py，但那个只吃 dict JSON，这里是 Markdown。）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    for attempt in range(3):
        try:
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                Path(tmp_name).replace(path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


def backup_file(path: Path) -> None:
    """改动前留档 `<file>.md.bak`（覆盖式，供人工回滚；`.bak` 不被 *.md 枚举误收）。"""
    if not path.exists():
        return
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(path.read_bytes())


class MemoryStore:
    """L2/L3 读写门面：路径/锁/加载/保存 + 条目级操作（都在锁内读改写）。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._locks: dict[str, asyncio.Lock] = {}

    def dir_for(self, layer: str) -> Path:
        return paths.memory_root(self._data_root) / ("L2" if layer == "l2" else "L3")

    def path_for(self, layer: str, key: str) -> Path:
        if layer == "l2":
            return paths.l2_path(self._data_root, key)
        return paths.l3_path(self._data_root, key)

    def lock_for(self, layer: str, key: str) -> asyncio.Lock:
        lock = self._locks.get(f"{layer}/{key}")
        if lock is None:
            lock = asyncio.Lock()
            self._locks[f"{layer}/{key}"] = lock
        return lock

    def load(self, layer: str, key: str) -> MemoryDoc:
        path = self.path_for(layer, key)
        if not path.exists():
            return MemoryDoc(layer=layer, key=key, header=default_header(layer, key))
        return parse_doc(path.read_text(encoding="utf-8"), layer=layer, key=key)

    def save(self, layer: str, key: str, doc: MemoryDoc) -> None:
        atomic_write_text(self.path_for(layer, key), render_doc(doc))

    async def apply(self, layer: str, key: str, mutate: Callable[[MemoryDoc], object]) -> MemoryDoc:
        """锁内「读 - 改 - 写」；mutate 就地把 doc 改好（勿在锁内 await）。"""
        async with self.lock_for(layer, key):
            doc = self.load(layer, key)
            mutate(doc)
            self.save(layer, key, doc)
            return doc

    def find(self, layer: str, entry_id: str) -> tuple[str, MemoryEntry] | None:
        """按 id 全文定位条目（PATCH/DELETE 用；文件都小，全扫可接受）。"""
        directory = self.dir_for(layer)
        if not directory.is_dir():
            return None
        for path in sorted(directory.glob("*.md")):
            doc = parse_doc(path.read_text(encoding="utf-8"), layer=layer, key=path.stem)
            for entry in doc.entries:
                if entry.id == entry_id:
                    return path.stem, entry
        return None

    def find_in(self, layer: str, key: str, entry_id: str) -> MemoryEntry | None:
        """已知文件键时按 id 精确查找（引用回读校验用）。"""
        for entry in self.load(layer, key).entries:
            if entry.id == entry_id:
                return entry
        return None

    async def append_entries(
        self, layer: str, key: str, entries: list[MemoryEntry]
    ) -> list[MemoryEntry]:
        """追加条目；按归一化正文去重（含批内），返回实际写入的（带分配 id）。"""
        written: list[MemoryEntry] = []

        def mutate(doc: MemoryDoc) -> None:
            seen = {normalize_text(entry.text) for entry in doc.entries}
            for entry in entries:
                normalized = normalize_text(entry.text)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                if not entry.id:
                    entry.id = new_id("mem")
                doc.entries.append(entry)
                written.append(entry)

        await self.apply(layer, key, mutate)
        return written

    async def update_entry(
        self,
        layer: str,
        key: str,
        entry_id: str,
        *,
        text: str | None = None,
    ) -> MemoryEntry | None:
        """改条目正文（保留 id/日期/引用）；找不到 id 返回 None。"""
        result: MemoryEntry | None = None

        def mutate(doc: MemoryDoc) -> None:
            nonlocal result
            for entry in doc.entries:
                if entry.id == entry_id:
                    if text is not None:
                        entry.text = text
                    result = entry
                    return

        await self.apply(layer, key, mutate)
        return result

    async def delete_entry(self, layer: str, key: str, entry_id: str) -> MemoryEntry | None:
        """删条目；返回被删的（供 tombstone），找不到返回 None。"""
        result: MemoryEntry | None = None

        def mutate(doc: MemoryDoc) -> None:
            nonlocal result
            for index, entry in enumerate(doc.entries):
                if entry.id == entry_id:
                    result = doc.entries.pop(index)
                    return

        await self.apply(layer, key, mutate)
        return result
