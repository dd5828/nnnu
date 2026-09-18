"""启动引导：data 目录树创建、schema 版本文件与幂等性。"""

from nnnu.runtime import bootstrap
from nnnu.runtime.home import get_data_root


def test_ensure_bootstrap_creates_tree(tmp_home):
    data_root = bootstrap.ensure_bootstrap()
    assert data_root == get_data_root()
    for sub in bootstrap.DATA_SUBDIRS:
        assert (data_root / sub).is_dir(), f"缺少目录 {sub}"
    schema_file = data_root / "system" / "schema_version.txt"
    assert schema_file.is_file()
    assert schema_file.read_text(encoding="utf-8").strip() == bootstrap.SCHEMA_VERSION


def test_ensure_bootstrap_idempotent(tmp_home):
    bootstrap.ensure_bootstrap()
    bootstrap.ensure_bootstrap()  # 二次调用不报错、不破坏内容
