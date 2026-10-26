"""MarginNote 4 接收端（§7.9，P14）：只做数据接收与浏览，不做 Add-on。

Add-on 在 MarginNote 里把学习数据（文档、摘录、脑图卡片与关系）打成增量批次，
带着配对令牌推到本机：

1. 用户在知识中心点「配对设备」，拿到一次性 token（服务端只存 sha256 摘要）；
2. Add-on 每次同步带 ``Authorization: MarginNote <device_id>:<token>``；
3. 服务按 ``object_id`` 幂等 upsert，删掉的 id 记墓碑；重复推送不会产生重复数据。

数据落 `data/user/external-libs/marginnote/`：设备、对象、游标各一份 JSON。
配对令牌只在第一次返回明文，之后任何接口都不回显。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nnnu.core.ids import new_id
from nnnu.services.audit import audit_log
from nnnu.services.settings.atomic import atomic_write_json

MN_TYPES = frozenset({"note", "excerpt", "card", "mindmap_node", "document", "comment"})
MAX_BATCH_OBJECTS = 2000
MAX_TEXT_CHARS = 200_000
MAX_TAGS = 100
MAX_LINKS = 500
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


class MarginNoteError(ValueError):
    """接收/查询参数不合法（消息直接给用户看）。"""


class MarginNoteAuthError(MarginNoteError):
    """设备令牌无效（401/403）。"""


@dataclass(slots=True)
class MnObject:
    object_id: str
    object_type: str
    title: str = ""
    content: str = ""
    excerpt: str | None = None
    document_id: str | None = None
    document_title: str | None = None
    page: int | None = None
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    color: str | None = None
    created_at: str = ""
    updated_at: str = ""
    synced_at: str = ""
    device_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MnDevice:
    device_id: str
    device_name: str
    device_kind: str
    token_hash: str
    paired_at: str
    last_seen: str
    active: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "device_kind": self.device_kind,
            "paired_at": self.paired_at,
            "last_seen": self.last_seen,
            "active": self.active,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MarginNoteService:
    def __init__(self, data_root: Path) -> None:
        self._root = data_root / "user" / "external-libs" / "marginnote"
        self._lock = asyncio.Lock()
        self._objects_cache: tuple[int, dict[str, dict[str, Any]]] | None = None

    # ================= 文件读写 =================

    @property
    def _devices_path(self) -> Path:
        return self._root / "devices.json"

    @property
    def _objects_path(self) -> Path:
        return self._root / "objects.json"

    @property
    def _cursors_path(self) -> Path:
        return self._root / "cursors.json"

    @staticmethod
    def _read_json(path: Path, key: str) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        value = payload.get(key) if isinstance(payload, dict) else None
        return value if isinstance(value, dict) else {}

    def _devices(self) -> dict[str, dict[str, Any]]:
        return self._read_json(self._devices_path, "devices")

    def _objects(self) -> dict[str, dict[str, Any]]:
        try:
            mtime = self._objects_path.stat().st_mtime_ns
        except OSError:
            return {}
        if self._objects_cache is not None and self._objects_cache[0] == mtime:
            return self._objects_cache[1]
        objects = self._read_json(self._objects_path, "objects")
        self._objects_cache = (mtime, objects)
        return objects

    def _save_devices(self, devices: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self._devices_path, {"devices": devices})

    def _save_objects(self, objects: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self._objects_path, {"objects": objects})
        try:
            self._objects_cache = (self._objects_path.stat().st_mtime_ns, objects)
        except OSError:
            self._objects_cache = None

    def _save_cursors(self, cursors: dict[str, str]) -> None:
        atomic_write_json(self._cursors_path, {"cursors": cursors})

    # ================= 设备 =================

    def list_devices(self) -> list[dict[str, Any]]:
        devices = self._devices()
        rows = [MnDevice(**row).public() for row in devices.values()]
        rows.sort(key=lambda item: (item["paired_at"], item["device_id"]))
        return rows

    def is_active(self) -> bool:
        """工具挂载门：至少有一台启用中的设备。"""
        return any(bool(row.get("active")) for row in self._devices().values())

    def count_objects(self) -> int:
        return len(self._objects())

    async def pair_device(
        self, *, device_name: str = "", device_kind: str = "macos"
    ) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(24)
        async with self._lock:
            devices = self._devices()
            device = MnDevice(
                device_id=new_id("mnd"),
                device_name=(device_name or "").strip() or "MarginNote 设备",
                device_kind=(device_kind or "macos").strip() or "macos",
                token_hash=_hash_token(token),
                paired_at=_now_iso(),
                last_seen="",
                active=True,
            )
            devices[device.device_id] = asdict(device)
            self._save_devices(devices)
            audit_log(
                "external_lib_marginnote_pair",
                device_id=device.device_id,
                device_kind=device.device_kind,
            )
            return device.public(), token

    def verify_device(self, device_id: str, token: str) -> bool:
        row = self._devices().get(device_id)
        if not row or not row.get("active"):
            return False
        expected = str(row.get("token_hash") or "")
        return bool(expected) and secrets.compare_digest(expected, _hash_token(token))

    async def revoke_device(self, device_id: str) -> dict[str, Any]:
        async with self._lock:
            devices = self._devices()
            row = devices.get(device_id)
            if row is None:
                raise MarginNoteError(f"设备不存在：{device_id}")
            row["active"] = False
            self._save_devices(devices)
            audit_log("external_lib_marginnote_revoke", device_id=device_id)
            return MnDevice(**row).public()

    def _touch_device(self, device_id: str) -> None:
        devices = self._devices()
        row = devices.get(device_id)
        if row is None:
            return
        row["last_seen"] = _now_iso()
        self._save_devices(devices)

    # ================= 同步接收 =================

    async def ingest(
        self,
        device_id: str,
        *,
        cursor: str = "",
        objects: list[MnObject],
        deleted_ids: list[str],
    ) -> dict[str, Any]:
        async with self._lock:
            if not self._devices().get(device_id, {}).get("active"):
                raise MarginNoteAuthError("设备未配对或已停用")
            if len(objects) > MAX_BATCH_OBJECTS or len(deleted_ids) > MAX_BATCH_OBJECTS:
                raise MarginNoteError(f"单批最多 {MAX_BATCH_OBJECTS} 条，请分批推送")
            current = self._objects()
            now = _now_iso()
            stored = updated = 0
            for obj in objects:
                _validate_object(obj)
                obj.synced_at = now
                obj.device_id = device_id
                if obj.object_id in current:
                    updated += 1
                else:
                    stored += 1
                current[obj.object_id] = obj.to_dict()
            deleted = 0
            for object_id in deleted_ids:
                if current.pop(str(object_id), None) is not None:
                    deleted += 1
            if stored or updated or deleted:
                self._save_objects(current)
            new_cursor = (cursor or "").strip() or f"{int(time.time() * 1000)}-{len(objects)}"
            cursors = self._read_json(self._cursors_path, "cursors")
            cursors[device_id] = new_cursor
            self._save_cursors(cursors)
            self._touch_device(device_id)
            return {
                "stored": stored,
                "updated": updated,
                "deleted": deleted,
                "new_cursor": new_cursor,
            }

    # ================= 浏览 =================

    def get(self, object_id: str) -> dict[str, Any] | None:
        return self._objects().get(str(object_id or "").strip())

    def search(self, query: str, *, object_type: str = "", limit: int = 20) -> list[dict[str, Any]]:
        needle = (query or "").strip().lower()
        if not needle:
            return []
        wanted = (object_type or "").strip()
        hits: list[dict[str, Any]] = []
        for row in sorted(
            self._objects().values(),
            key=lambda item: (item.get("updated_at") or "", item.get("object_id") or ""),
            reverse=True,
        ):
            if wanted and row.get("object_type") != wanted:
                continue
            text = " ".join(
                str(row.get(key) or "") for key in ("title", "content", "excerpt", "document_title")
            )
            index = text.lower().find(needle)
            if index < 0 and not any(needle in str(tag).lower() for tag in row.get("tags") or []):
                continue
            snippet = text[max(0, index - 40) : index + 120].replace("\n", " ").strip()
            hits.append(
                {
                    "object_id": row.get("object_id"),
                    "object_type": row.get("object_type"),
                    "title": row.get("title") or "",
                    "snippet": snippet,
                    "document_id": row.get("document_id"),
                    "document_title": row.get("document_title"),
                    "page": row.get("page"),
                }
            )
            if len(hits) >= _clamp(limit, 1, MAX_LIMIT):
                break
        return hits

    def list_objects(
        self, *, object_type: str = "", document_id: str = "", limit: int = DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        wanted = (object_type or "").strip()
        document = (document_id or "").strip()
        rows = [
            row
            for row in self._objects().values()
            if (not wanted or row.get("object_type") == wanted)
            and (not document or row.get("document_id") == document)
        ]
        rows.sort(
            key=lambda item: (item.get("updated_at") or "", item.get("object_id") or ""),
            reverse=True,
        )
        return rows[: _clamp(limit, 1, MAX_LIMIT)]

    def linked_objects(self, object_id: str) -> list[dict[str, Any]]:
        identifier = str(object_id or "").strip()
        target = self.get(identifier)
        if target is None:
            raise MarginNoteError(f"对象不存在：{identifier}")
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for linked_id in target.get("links") or []:
            row = self._objects().get(str(linked_id))
            if row is None or ("out", str(linked_id)) in seen:
                continue
            seen.add(("out", str(linked_id)))
            rows.append(_link_row(row, direction="out"))
        for row in self._objects().values():
            if identifier in [str(item) for item in row.get("links") or []]:
                key = ("in", str(row.get("object_id")))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(_link_row(row, direction="in"))
        return rows

    def documents(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in self._objects().values():
            document_id = str(row.get("document_id") or "").strip()
            if not document_id:
                continue
            group = groups.setdefault(
                document_id,
                {
                    "document_id": document_id,
                    "title": str(row.get("document_title") or document_id),
                    "objects": 0,
                    "notes": 0,
                    "cards": 0,
                    "nodes": 0,
                },
            )
            group["objects"] += 1
            if row.get("object_type") in {"note", "excerpt", "comment"}:
                group["notes"] += 1
            elif row.get("object_type") == "card":
                group["cards"] += 1
            elif row.get("object_type") == "mindmap_node":
                group["nodes"] += 1
            if row.get("object_type") == "document" and row.get("title"):
                group["title"] = str(row["title"])
        rows = list(groups.values())
        rows.sort(key=lambda item: (-int(item["objects"]), str(item["title"])))
        return rows

    def tags(self, *, limit: int = 200) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in self._objects().values():
            for tag in row.get("tags") or []:
                name = str(tag).strip()
                if name:
                    counts[name] = counts.get(name, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [
            {"tag": tag, "count": count} for tag, count in ranked[: _clamp(limit, 1, MAX_LIMIT)]
        ]

    def status(self) -> dict[str, Any]:
        devices = self.list_devices()
        cursors = self._read_json(self._cursors_path, "cursors")
        return {
            "devices": len(devices),
            "active_devices": sum(1 for row in devices if row["active"]),
            "objects": self.count_objects(),
            "last_seen": max((row["last_seen"] or "" for row in devices), default=""),
            "cursors": cursors,
        }


def _validate_object(obj: MnObject) -> None:
    if not str(obj.object_id or "").strip():
        raise MarginNoteError("object_id 不能为空")
    if obj.object_type not in MN_TYPES:
        raise MarginNoteError(f"未知对象类型 {obj.object_type!r}，取 {sorted(MN_TYPES)} 之一")
    for name in ("title", "content", "excerpt"):
        value = getattr(obj, name)
        if value and len(value) > MAX_TEXT_CHARS:
            raise MarginNoteError(f"{name} 超过 {MAX_TEXT_CHARS} 字符，请拆分后推送")
    if len(obj.tags) > MAX_TAGS or len(obj.links) > MAX_LINKS:
        raise MarginNoteError("标签或关系数量过多")


def _link_row(row: dict[str, Any], *, direction: str) -> dict[str, Any]:
    return {
        "object_id": row.get("object_id"),
        "object_type": row.get("object_type"),
        "title": row.get("title") or "",
        "document_title": row.get("document_title"),
        "page": row.get("page"),
        "direction": direction,
    }


def _clamp(value: Any, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return hi
    return max(lo, min(number, hi))


_marginnote_service: MarginNoteService | None = None


def set_marginnote_service(service: MarginNoteService | None) -> None:
    global _marginnote_service
    _marginnote_service = service


def get_marginnote_service() -> MarginNoteService:
    if _marginnote_service is None:
        raise RuntimeError("MarginNote 服务尚未装配")
    return _marginnote_service
