"""启动引导：data 目录树创建与幂等性（schema 版本文件归 services/sessions/schema.py）。"""

from nnnu.runtime import bootstrap
from nnnu.runtime.home import get_data_root


def test_ensure_bootstrap_creates_tree(tmp_home):
    data_root = bootstrap.ensure_bootstrap()
    assert data_root == get_data_root()
    for sub in bootstrap.DATA_SUBDIRS:
        assert (data_root / sub).is_dir(), f"缺少目录 {sub}"
    # schema 版本文件不再由 bootstrap 写（§8.4 单写者）
    assert not (data_root / "system" / "schema_version.txt").exists()


def test_ensure_bootstrap_idempotent(tmp_home):
    bootstrap.ensure_bootstrap()
    bootstrap.ensure_bootstrap()  # 二次调用不报错、不破坏内容
