"""CLI 会话续跑记录（§7.12）：主会话 + 连接 → CLI 侧的 session_id。

外部 CLI 自己维护着上下文（`claude --resume <id>` 能接着上次聊）。我们只记这一张
映射表：下一次**同一条主会话、同一条连接**的咨询带上 `--resume`，子代理就不用每次
从头了解背景。键是 `"{主会话 id}::{连接 id}"`——同一条会话换了连接，是另一段上下文，
不能串。

写路径与 spec.py 同一套（原子写 + 进程内锁，读-改-写并发会互相覆盖）。额外一条
**封顶**：这份表只会随会话数增长，而绝大多数条目此后再也不会被读到（会话 id 是随机
的，不会复用），超过 `MAX_ENTRIES` 就按最后使用时间丢最旧的。

CLI 说这个会话没了（过期/被删）时，调用方 `forget` 掉再重开——见 runner 的
`stale_session`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field

from nnnu.runtime import home
from nnnu.services.settings.atomic import atomic_write_json

FILENAME = "cli_sessions.json"
#: 表里最多留几条（按最后使用时间淘汰最旧的）
MAX_ENTRIES = 200

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


class CliSessionEntry(BaseModel):
    cli_session_id: str = ""
    updated_at: float = 0.0


class CliSessionTable(BaseModel):
    entries: dict[str, CliSessionEntry] = Field(default_factory=dict)


def config_path() -> Path:
    return home.get_data_root() / "user" / "agents" / FILENAME


def _key(session_id: str, connection_id: str) -> str:
    return f"{session_id}::{connection_id}"


def _load() -> CliSessionTable:
    path = config_path()
    if not path.is_file():
        return CliSessionTable()
    try:
        return CliSessionTable.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        # 这张表是「省一次背景介绍」的优化，坏了就重开一轮，不拦咨询
        logger.warning("子代理续跑记录损坏，按空处理：%s", path)
        return CliSessionTable()


def lookup(session_id: str, connection_id: str) -> str:
    """上次这条会话+连接用的 CLI 会话 id（没有就空串 = 新开一段）。"""
    if not session_id or not connection_id:
        return ""
    return _load().entries.get(_key(session_id, connection_id), CliSessionEntry()).cli_session_id


async def remember(session_id: str, connection_id: str, cli_session_id: str) -> None:
    """记下这次用的 CLI 会话 id（空值 = 什么都没记住，直接返回）。"""
    if not session_id or not connection_id or not cli_session_id:
        return
    async with _lock:
        table = _load()
        table.entries[_key(session_id, connection_id)] = CliSessionEntry(
            cli_session_id=cli_session_id, updated_at=time.time()
        )
        _prune(table)
        atomic_write_json(config_path(), table.model_dump(mode="json"))


async def forget(session_id: str, connection_id: str) -> None:
    """忘掉这条映射（CLI 那边说会话没了时调用）。"""
    async with _lock:
        table = _load()
        if table.entries.pop(_key(session_id, connection_id), None) is None:
            return
        atomic_write_json(config_path(), table.model_dump(mode="json"))


def _prune(table: CliSessionTable) -> None:
    """超过上限就丢最旧的：这张表只会增长，而旧条目基本再也读不到。"""
    if len(table.entries) <= MAX_ENTRIES:
        return
    ordered = sorted(table.entries.items(), key=lambda item: item[1].updated_at, reverse=True)
    table.entries = dict(ordered[:MAX_ENTRIES])
