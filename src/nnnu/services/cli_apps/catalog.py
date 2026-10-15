"""命令行应用目录（§7.18）：内置清单 + 用户自加条目。

内置清单是 `catalogs/cli_apps.json`（随 prompts/ 一起装），按**文件 mtime 缓存**：
目录是静态数据，但测试会临时换一份，用 mtime 当缓存键就不用给测试开后门。
条目坏了（手改出格）只丢那一条并记日志——一个错字不该让整页目录消失。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nnnu.runtime import home
from nnnu.services.cli_apps.spec import CliAppEntry, parse_entry
from nnnu.services.settings.atomic import atomic_write_json

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "cli_apps.json"  # catalogs/cli_apps.json（内置）
USER_APPS_FILENAME = "apps.json"  # data/user/cli-apps/apps.json（用户自加 + 安装记录）

#: mtime →（条目, meta）。meta 一起缓存：界面页脚要显示目录的来源与 commit（署名）
_catalog_cache: tuple[float, list[CliAppEntry], dict[str, Any]] | None = None


def user_apps_path() -> Path:
    return home.get_data_root() / "user" / "cli-apps" / USER_APPS_FILENAME


def app_dir(app_id: str) -> Path:
    """一件应用的安装目录（venv / node_modules / install.log 都在这儿）。"""
    return home.get_data_root() / "user" / "cli-apps" / app_id


def read_user_file() -> dict[str, Any]:
    """读 `apps.json`；缺失/坏掉按空处理（安装记录丢了最多重装一次）。"""
    path = user_apps_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("CLI 应用记录损坏，按空处理：%s", path)
        return {}
    return raw if isinstance(raw, dict) else {}


def write_user_file(data: dict[str, Any]) -> None:
    atomic_write_json(user_apps_path(), data)


def custom_entries() -> list[CliAppEntry]:
    """用户自己加的条目（同 id 覆盖内置的）。"""
    raw = read_user_file().get("custom") or []
    entries: list[CliAppEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(parse_entry(item, origin="local"))
        except ValueError as exc:
            logger.warning("用户自加的 CLI 条目坏了，已跳过：%s", exc)
    return entries


def save_custom_entries(entries: list[CliAppEntry]) -> None:
    data = read_user_file()
    data["custom"] = [entry.as_dict() for entry in entries]
    write_user_file(data)


def catalog_entries() -> list[CliAppEntry]:
    """内置目录；文件不在/坏了返回空列表（目录是可选扩展，不该拦任何流程）。"""
    return _load_catalog()[0]


def catalog_meta() -> dict[str, Any]:
    """目录的来源信息（source / commit / 收录口径），界面页脚与排障用。"""
    return _load_catalog()[1]


def _load_catalog() -> tuple[list[CliAppEntry], dict[str, Any]]:
    global _catalog_cache
    path = home.get_catalogs_root() / CATALOG_FILENAME
    if not path.is_file():
        logger.warning("内置 CLI 应用目录不存在：%s", path)
        return [], {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return [], {}
    if _catalog_cache is not None and _catalog_cache[0] == mtime:
        return _catalog_cache[1], _catalog_cache[2]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("内置 CLI 应用目录损坏：%s", path)
        return [], {}
    items = raw.get("apps") if isinstance(raw, dict) else raw
    entries: list[CliAppEntry] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(parse_entry(item, origin="catalog"))
        except ValueError as exc:
            logger.warning("内置 CLI 条目坏了，已跳过：%s", exc)
    meta = raw.get("meta") if isinstance(raw, dict) else {}
    _catalog_cache = (mtime, entries, meta if isinstance(meta, dict) else {})
    return entries, _catalog_cache[2]


def all_entries() -> list[CliAppEntry]:
    """内置 + 自加（自加同 id 覆盖内置：用户改过的就是他要的那份）。"""
    merged: dict[str, CliAppEntry] = {entry.id: entry for entry in catalog_entries()}
    for entry in custom_entries():
        merged[entry.id] = entry
    return list(merged.values())


def get_entry(app_id: str) -> CliAppEntry | None:
    for entry in all_entries():
        if entry.id == app_id:
            return entry
    return None


def search(query: str = "", *, source: list[CliAppEntry] | None = None) -> list[CliAppEntry]:
    """子串搜索（id/名称/说明/分类，不区分大小写）；空查询 = 全部。"""
    entries = source if source is not None else all_entries()
    text = (query or "").strip().lower()
    if not text:
        return list(entries)
    hits: list[CliAppEntry] = []
    for entry in entries:
        haystack = " ".join(
            [
                entry.id,
                entry.name,
                entry.category,
                entry.entry_point,
                " ".join(entry.description.values()),
                " ".join(entry.requires.values()),
            ]
        ).lower()
        if text in haystack:
            hits.append(entry)
    return hits
