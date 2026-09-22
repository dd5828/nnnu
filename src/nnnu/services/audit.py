"""审计日志（§11.6）：关键操作单独落 data/system/audit.log，永不轮转。

与运行日志分文件：运行日志是给调试看的、会轮转；审计要能回答「什么时候
谁删了哪个知识库」，所以 append-only、不清理。单行一条 JSON。

P4 记录：KB 创建/删除、文档上传/删除、重建与取消。写失败只告警不抛——
审计不该把主流程拖挂，但也不能装没看见（logger 会喊）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def audit_path(home: str | Path | None = None) -> Path:
    from nnnu.runtime import home as runtime_home

    return runtime_home.get_data_root(home) / "system" / "audit.log"


def audit_log(action: str, **fields: Any) -> None:
    """追加一条审计记录（append + flush；失败只告警）。"""
    entry = {"ts": time.time(), "action": action, **fields}
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as exc:
        logger.warning("审计日志写不进去（%s）：%s", path, exc)


def read_audit(limit: int = 200, *, home: str | Path | None = None) -> list[dict[str, Any]]:
    """读最近 limit 条（给测试与将来的审计页用）；坏行跳过。"""
    path = audit_path(home)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries
