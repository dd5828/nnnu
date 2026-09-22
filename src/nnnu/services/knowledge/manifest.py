"""KB 磁盘布局与原子协议（§7.9 版本化 / §8.3）。

```
data/user/knowledge/<kb_id>/
  manifest.json                 唯一权威，原子写（tmp + fsync + replace）
  index/version-N/              一次构建一个目录
    chunks.jsonl  embeddings.npy  bm25.json   ← 引擎写的三件套
    meta.json                   ← **最后**写，ready=true 是「这个版本可用」的定义
data/user/uploads/kb/<kb_id>/<doc_id>/{original.<ext>, parsed.json}
```

原子切换（验收 C）：新版本先在旁边的 version-(N+1) 里全量写完，落地 meta.json
（ready）之后，才原子改 manifest 的 active_version。任何时刻的崩溃都只会留下
一个「没写 meta 的半成品目录」和一个「指向上一个完整版本」的指针——查询永远
打在完整版本上。半成品目录由 recover_stale() 在启动时清掉。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from nnnu.services.knowledge.types import KbManifest
from nnnu.services.settings.atomic import atomic_write_json

logger = logging.getLogger(__name__)

MANIFEST_FILE = "manifest.json"
INDEX_DIR = "index"
VERSION_DIR_TEMPLATE = "version-{version}"
META_FILE = "meta.json"

KB_ID_PATTERN = re.compile(r"^kb-[0-9a-f]{8}$")
DOC_ID_PATTERN = re.compile(r"^kbdoc-[0-9a-f]{8}$")

# §11.3 fail-closed：目录名与文件名都不允许出现路径分隔与控制字符
_FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*#%')
MAX_NAME_CHARS = 120


class KbNameError(ValueError):
    """KB 名称非法（空、超长、含路径字符）。"""


def validate_kb_name(name: str) -> str:
    """KB 名校验并归一（NFC）；目录名一律用 kb-xxxx，所以这里只管显示名。"""
    normalized = unicodedata.normalize("NFC", (name or "").strip())
    if not normalized:
        raise KbNameError("知识库名不能为空")
    if len(normalized) > MAX_NAME_CHARS:
        raise KbNameError(f"知识库名最多 {MAX_NAME_CHARS} 个字符")
    if any(char in _FORBIDDEN_NAME_CHARS for char in normalized):
        raise KbNameError('知识库名不能包含 < > : " / \\ | ? * # % 这些字符')
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise KbNameError("知识库名不能包含控制字符")
    return normalized


def validate_kb_id(kb_id: str) -> str:
    if not KB_ID_PATTERN.match(kb_id or ""):
        raise KbNameError(f"非法知识库 id：{kb_id!r}")
    return kb_id


def validate_doc_id(doc_id: str) -> str:
    if not DOC_ID_PATTERN.match(doc_id or ""):
        raise KbNameError(f"非法文档 id：{doc_id!r}")
    return doc_id


def knowledge_root(data_root: Path) -> Path:
    return data_root / "user" / "knowledge"


def kb_dir(data_root: Path, kb_id: str) -> Path:
    return knowledge_root(data_root) / validate_kb_id(kb_id)


def manifest_path(data_root: Path, kb_id: str) -> Path:
    return kb_dir(data_root, kb_id) / MANIFEST_FILE


def version_dir(data_root: Path, kb_id: str, version: int) -> Path:
    return kb_dir(data_root, kb_id) / INDEX_DIR / VERSION_DIR_TEMPLATE.format(version=version)


def upload_dir(data_root: Path, kb_id: str, doc_id: str) -> Path:
    return data_root / "user" / "uploads" / "kb" / validate_kb_id(kb_id) / validate_doc_id(doc_id)


# ---- manifest 读写 ----


def read_manifest(data_root: Path, kb_id: str) -> KbManifest | None:
    path = manifest_path(data_root, kb_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("manifest 读不出来：%s（%s）", path, exc)
        return None
    try:
        return KbManifest.model_validate(raw)
    except Exception as exc:  # 字段对不上：当作损坏，别把整个知识模块拖崩
        logger.error("manifest 格式不符：%s（%s）", path, exc)
        return None


def write_manifest(data_root: Path, manifest: KbManifest) -> None:
    manifest.updated_at = _now()
    atomic_write_json(manifest_path(data_root, manifest.id), manifest.model_dump())


def list_kb_ids(data_root: Path) -> list[str]:
    root = knowledge_root(data_root)
    if not root.is_dir():
        return []
    return sorted(
        item.name
        for item in root.iterdir()
        if item.is_dir() and KB_ID_PATTERN.match(item.name) and (item / MANIFEST_FILE).exists()
    )


# ---- 版本就绪标志 ----


def write_ready_meta(index_dir: Path, payload: dict[str, Any]) -> None:
    """写 meta.json——**必须最后调**：它一落地，这个版本就允许被切换为活跃版本。"""
    atomic_write_json(index_dir / META_FILE, {**payload, "ready": True})


def read_meta(index_dir: Path) -> dict[str, Any] | None:
    path = index_dir / META_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def is_version_ready(index_dir: Path) -> bool:
    meta = read_meta(index_dir)
    return bool(meta and meta.get("ready") is True)


def prune_unready_versions(
    data_root: Path, kb_id: str, *, keep: set[int] | None = None
) -> list[int]:
    """删掉没有 ready meta 的版本目录（构建中断的残骸）。返回删掉的版本号。"""
    keep = keep or set()
    directory = kb_dir(data_root, kb_id) / INDEX_DIR
    if not directory.is_dir():
        return []
    removed: list[int] = []
    for item in directory.iterdir():
        if not item.is_dir() or not item.name.startswith("version-"):
            continue
        try:
            version = int(item.name.split("-", 1)[1])
        except ValueError:
            continue
        if version in keep or is_version_ready(item):
            continue
        remove_tree(item)
        removed.append(version)
    return removed


def remove_version(data_root: Path, kb_id: str, version: int) -> None:
    remove_tree(version_dir(data_root, kb_id, version))


def remove_tree(path: Path) -> None:
    """递归删目录（Windows 上句柄占用会导致个别文件删不掉：告警跳过，不影响正确性）。"""
    if not path.is_dir():
        return
    for item in sorted(path.rglob("*"), reverse=True):
        try:
            item.unlink() if item.is_file() else item.rmdir()
        except OSError:  # 句柄被占（Windows 上常见）：留着不影响正确性
            logger.warning("删不掉 %s，跳过", item)
    try:
        path.rmdir()
    except OSError:
        logger.warning("版本目录 %s 没删干净", path)


def _now() -> float:
    import time

    return time.time()
