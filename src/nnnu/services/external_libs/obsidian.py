"""Obsidian 库接入（§7.9，P14）：用户指定 vault 路径，原位读写笔记。

不复制、不导入索引：vault 就是用户自己的目录，我们只是按 Obsidian 的约定
读 `.md`、写 `.md`。三个约定写死在这里：

- 只列/读/写笔记文件（`.md`），vault 内部的 `.obsidian`、`.trash`、`.git`
  一律跳过；
- 所有路径都相对 vault 根，`..`、绝对路径、指向 vault 外的符号链接一律拒绝
  （`_safe_join`）——写接口不能变成任意文件写；
- 读允许坏字节（按替换符显示，vault 常被不同编辑器改过），但「追加」要
  把原文写回去，所以追加前严格按 UTF-8 解码，坏字节就报错而不是悄悄覆盖。

挂载清单只有 `data/user/external-libs/obsidian.json` 一份；路径以挂载时的
绝对路径为准，vault 被移动/删除后操作会给出明确错误，而不是当空目录。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nnnu.core.ids import new_id
from nnnu.services.audit import audit_log
from nnnu.services.settings.atomic import atomic_write_json, atomic_write_text

VAULTS_FILE = "obsidian.json"
NOTE_SUFFIX = ".md"
MAX_NOTE_BYTES = 4 * 1024 * 1024
MAX_LIST_NOTES = 1000
MAX_SEARCH_HITS = 50
SNIPPET_CHARS = 160
IGNORED_DIRS = frozenset({".obsidian", ".trash", ".git"})


class VaultError(ValueError):
    """vault 操作失败（路径越界、笔记不存在、文件太大……消息直接给用户）。"""


class VaultNotFound(VaultError):
    """这个 vault 没有挂载过（或已移除）。"""


class NoteNotFound(VaultError):
    """vault 里找不到这篇笔记。"""


@dataclass(slots=True)
class Vault:
    vault_id: str
    name: str
    path: str
    added_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_id": self.vault_id,
            "name": self.name,
            "path": self.path,
            "added_at": self.added_at,
        }


def _sources_path(data_root: Path) -> Path:
    return data_root / "user" / "external-libs" / VAULTS_FILE


def _read_vaults(data_root: Path) -> list[Vault]:
    path = _sources_path(data_root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("vaults") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    vaults: list[Vault] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            vaults.append(
                Vault(
                    vault_id=str(row["vault_id"]),
                    name=str(row.get("name") or ""),
                    path=str(row["path"]),
                    added_at=float(row.get("added_at") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return vaults


def _write_vaults(data_root: Path, vaults: list[Vault]) -> None:
    atomic_write_json(_sources_path(data_root), {"vaults": [vault.to_dict() for vault in vaults]})


# ================= 纯文件操作 =================


def vault_root(vault: Vault) -> Path:
    """当前可用的 vault 根目录；被移动/删除时抛 VaultError。"""
    try:
        root = Path(vault.path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"vault 路径不存在或无法访问：{vault.path}") from exc
    if not root.is_dir():
        raise VaultError(f"vault 路径不是目录：{vault.path}")
    return root


def _safe_join(root: Path, rel: str) -> Path:
    raw = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        raise VaultError("笔记路径不能为空")
    if "\x00" in raw:
        raise VaultError("笔记路径包含非法字符")
    # 盘符路径在 Windows 上会被 Path 当成绝对路径，Linux 上却只是普通相对名
    # （会悄悄在 vault 里建一个 C: 目录）。跨平台一律按越界拒绝。
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise VaultError(f"笔记路径越出了 vault：{rel}")
    candidate = (root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise VaultError(f"笔记路径越出了 vault：{rel}") from exc
    return candidate


def _note_path(root: Path, ref: str) -> Path:
    clean = (ref or "").strip().replace("\\", "/")
    if not clean:
        raise VaultError("笔记路径不能为空")
    if not clean.lower().endswith(NOTE_SUFFIX):
        clean += NOTE_SUFFIX
    return _safe_join(root, clean)


def _is_ignored(root: Path, path: Path) -> bool:
    try:
        parts = path.resolve().relative_to(root).parts
    except (OSError, ValueError):
        return True  # 指到 vault 外的符号链接：不列不读
    return any(part in IGNORED_DIRS for part in parts)


def _iter_notes(root: Path, base: Path | None = None):
    for path in sorted((base or root).rglob(f"*{NOTE_SUFFIX}")):
        if _is_ignored(root, path) or not path.is_file():
            continue
        yield path


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def list_notes_sync(
    root: Path, folder: str = "", limit: int = MAX_LIST_NOTES
) -> list[dict[str, Any]]:
    base = root
    if (folder or "").strip():
        base = _safe_join(root, folder)
        if not base.is_dir():
            return []
    rows: list[dict[str, Any]] = []
    for path in _iter_notes(root, base):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append(
            {
                "path": _rel(root, path),
                "title": path.stem,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
        if len(rows) >= max(1, min(limit, MAX_LIST_NOTES)):
            break
    return rows


def read_note_sync(root: Path, ref: str) -> dict[str, Any]:
    path = _note_path(root, ref)
    if not path.is_file():
        raise NoteNotFound(f"vault 里找不到笔记：{ref}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise VaultError(f"读取笔记失败：{exc}") from exc
    if size > MAX_NOTE_BYTES:
        raise VaultError(f"笔记超过 {MAX_NOTE_BYTES // (1024 * 1024)}MB，暂不支持在应用里打开")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise VaultError(f"读取笔记失败：{exc}") from exc
    return {
        "path": _rel(root, path),
        "title": path.stem,
        "content": text,
        "size": size,
        "modified_at": path.stat().st_mtime,
    }


def write_note_sync(
    root: Path, ref: str, content: str, *, mode: str = "overwrite"
) -> dict[str, Any]:
    """写回笔记：overwrite 覆盖全文，append 追加到末尾。

    `append` 只对已有笔记做读-改-写，且必须先严格解码成功——否则会把用户
    原文里的坏字节替换成 U+FFFD 再写回去。
    """
    if mode not in {"overwrite", "append"}:
        raise VaultError("写入方式只能是 overwrite 或 append")
    encoded = (content or "").encode("utf-8")
    if len(encoded) > MAX_NOTE_BYTES:
        raise VaultError(f"内容超过 {MAX_NOTE_BYTES // (1024 * 1024)}MB，未写入")
    path = _note_path(root, ref)
    created = not path.exists()
    if mode == "append" and not created:
        try:
            existing = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise VaultError(
                f"笔记《{path.stem}》不是合法的 UTF-8（字节偏移 {exc.start}），"
                "追加会把坏字节覆盖掉；请先在 Obsidian 里另存为 UTF-8"
            ) from exc
        except OSError as exc:
            raise VaultError(f"读取笔记失败：{exc}") from exc
        separator = "" if not existing or existing.endswith("\n") else "\n"
        result = existing + separator + content
    else:
        result = content or ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VaultError(f"创建笔记目录失败：{exc}") from exc
    try:
        atomic_write_text(path, result)
    except OSError as exc:
        raise VaultError(f"写入笔记失败：{exc}") from exc
    return {
        "path": _rel(root, path),
        "title": path.stem,
        "created": created,
        "mode": mode,
        "size": len(result.encode("utf-8")),
    }


def search_notes_sync(
    root: Path, query: str, *, limit: int = MAX_SEARCH_HITS
) -> list[dict[str, str]]:
    needle = (query or "").strip().lower()
    if not needle:
        return []
    hits: list[dict[str, str]] = []
    for path in _iter_notes(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        haystack = f"{path.stem}\n{text}".lower()
        index = haystack.find(needle)
        if index < 0:
            continue
        start = max(0, index - SNIPPET_CHARS // 3)
        snippet = text[start : start + SNIPPET_CHARS].replace("\n", " ").strip()
        if start:
            snippet = "…" + snippet
        if start + SNIPPET_CHARS < len(text):
            snippet += "…"
        hits.append({"path": _rel(root, path), "snippet": snippet})
        if len(hits) >= max(1, min(limit, MAX_SEARCH_HITS)):
            break
    return hits


# ================= 服务 =================


class ObsidianService:
    """vault 挂载清单 + 笔记操作入口（路由与工具共用）。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._lock = asyncio.Lock()

    @property
    def data_root(self) -> Path:
        return self._data_root

    def list_vaults(self) -> list[Vault]:
        return _read_vaults(self._data_root)

    def get_vault(self, vault_id: str) -> Vault:
        for vault in self.list_vaults():
            if vault.vault_id == vault_id:
                return vault
        raise VaultNotFound(f"这个 Obsidian 库不存在（可能已移除）：{vault_id}")

    async def add_vault(self, path: str, *, name: str = "") -> Vault:
        raw = (path or "").strip()
        if not raw:
            raise VaultError("vault 路径不能为空")
        try:
            resolved = Path(raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise VaultError(f"vault 路径不存在或无法访问：{raw}") from exc
        if not resolved.is_dir():
            raise VaultError(f"vault 路径不是目录：{raw}")
        async with self._lock:
            vaults = self.list_vaults()
            if any(Path(existing.path) == resolved for existing in vaults):
                raise VaultError("这个 vault 已经挂载过了")
            vault = Vault(
                vault_id=new_id("vault"),
                name=(name or "").strip() or resolved.name or "Obsidian 库",
                path=str(resolved),
                added_at=time.time(),
            )
            vaults.append(vault)
            _write_vaults(self._data_root, vaults)
            audit_log("external_lib_vault_add", vault_id=vault.vault_id, name=vault.name)
            return vault

    async def remove_vault(self, vault_id: str) -> Vault:
        async with self._lock:
            vaults = self.list_vaults()
            found: Vault | None = None
            remaining: list[Vault] = []
            for vault in vaults:
                if vault.vault_id == vault_id:
                    found = vault
                else:
                    remaining.append(vault)
            if found is None:
                raise VaultNotFound(f"这个 Obsidian 库不存在（可能已移除）：{vault_id}")
            _write_vaults(self._data_root, remaining)
            audit_log("external_lib_vault_remove", vault_id=vault_id)
            return found

    async def list_notes(
        self, vault_id: str, *, folder: str = "", limit: int = MAX_LIST_NOTES
    ) -> dict[str, Any]:
        vault = self.get_vault(vault_id)
        root = vault_root(vault)
        notes = await asyncio.to_thread(list_notes_sync, root, folder, limit)
        return {"vault": vault.to_dict(), "notes": notes, "folder": folder}

    async def read_note(self, vault_id: str, ref: str) -> dict[str, Any]:
        vault = self.get_vault(vault_id)
        root = vault_root(vault)
        note = await asyncio.to_thread(read_note_sync, root, ref)
        return {"vault": vault.to_dict(), **note}

    async def write_note(
        self, vault_id: str, ref: str, content: str, *, mode: str = "overwrite"
    ) -> dict[str, Any]:
        vault = self.get_vault(vault_id)
        root = vault_root(vault)
        result = await asyncio.to_thread(write_note_sync, root, ref, content, mode=mode)
        audit_log(
            "external_lib_note_write",
            vault_id=vault_id,
            path=result["path"],
            mode=mode,
            created=result["created"],
        )
        return {"vault": vault.to_dict(), **result}

    async def search(
        self, vault_id: str, query: str, *, limit: int = MAX_SEARCH_HITS
    ) -> dict[str, Any]:
        vault = self.get_vault(vault_id)
        root = vault_root(vault)
        hits = await asyncio.to_thread(search_notes_sync, root, query, limit=limit)
        return {"vault": vault.to_dict(), "hits": hits, "query": query}


_obsidian_service: ObsidianService | None = None


def set_obsidian_service(service: ObsidianService | None) -> None:
    global _obsidian_service
    _obsidian_service = service


def get_obsidian_service() -> ObsidianService:
    if _obsidian_service is None:
        raise RuntimeError("Obsidian 服务尚未装配")
    return _obsidian_service
