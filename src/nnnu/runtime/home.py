"""工作区解析：NNNU_HOME、data 根目录与 prompts 根目录定位。"""

import os
from pathlib import Path


def get_runtime_home(explicit: str | Path | None = None) -> Path:
    """解析运行时 home 目录：显式参数 > NNNU_HOME > 当前工作目录。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get("NNNU_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return Path.cwd()


def get_data_root(home: str | Path | None = None) -> Path:
    """data 根目录 = <home>/data（全部运行时数据落在这里）。"""
    return get_runtime_home(home) / "data"


def get_prompts_root(home: str | Path | None = None) -> Path:
    """prompts 根目录：<home>/prompts 存在则用之，否则回退仓库根 prompts。

    开发态 home 即仓库根，直接命中；安装态（pip install / Docker COPY）
    依赖调用方把 prompts 放到 home 下，回退路径只是开发兜底。
    """
    home_dir = get_runtime_home(home)
    home_prompts = home_dir / "prompts"
    if home_prompts.is_dir():
        return home_prompts
    repo_prompts = Path(__file__).resolve().parents[3] / "prompts"
    if repo_prompts.is_dir():
        return repo_prompts
    return home_prompts  # 目录不存在时由加载器降级处理
