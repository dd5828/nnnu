"""手写 .env 加载器（§5 配置铁律：.env 仅开发环境兜底）。

不引入 python-dotenv：需求只是 KEY=VALUE/引号/注释三件事，约 30 行。
规则：已存在的环境变量绝不被 .env 覆盖（显式环境优先）。
"""

import os
from pathlib import Path


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """加载 .env（默认仓库根/.env），返回本次新写入的环境变量。"""
    dotenv_path = path if path is not None else Path.cwd() / ".env"
    if not dotenv_path.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 首尾配对引号剥掉；注释只认整行，行尾注释按字面值保留
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        # 空值行跳过（空值 = 未设置意图）；已存在的环境变量绝不被覆盖
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
