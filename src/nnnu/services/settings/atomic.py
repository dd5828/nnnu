"""JSON 原子写公共工具（§8.3）：settings 与 secrets 共用。

同目录临时文件 + fsync + Path.replace；Windows 下 PermissionError
（杀软/句柄瞬锁）指数退避重试。
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: dict[str, Any], *, chmod: int | None = None) -> None:
    """原子写 JSON 文件；chmod 供 secrets 等敏感文件收紧权限（POSIX 生效）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for attempt in range(3):
        try:
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                if chmod is not None:
                    os.chmod(tmp_name, chmod)
                Path(tmp_name).replace(path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
