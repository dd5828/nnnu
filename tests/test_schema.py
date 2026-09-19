"""schema 迁移：全新安装、v1→v2、幂等、失败中止。"""

import sqlite3

import pytest

from nnnu.services.sessions.schema import MIGRATIONS, SCHEMA_VERSION, db_path, migrate


def _version_file(tmp_home):
    return tmp_home / "data" / "system" / "schema_version.txt"


def _table_names(tmp_home) -> set[str]:
    conn = sqlite3.connect(db_path(tmp_home / "data"))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def test_fresh_install_creates_v2(tmp_home):
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert _version_file(tmp_home).read_text(encoding="utf-8").strip() == "2"
    tables = _table_names(tmp_home)
    assert {"sessions", "messages", "usage_records"} <= tables


def test_migrate_v1_to_v2(tmp_home):
    # 模拟 P0 状态：版本文件 v1、无表
    (tmp_home / "data" / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("1\n", encoding="utf-8")
    assert migrate(tmp_home / "data") == "2"
    assert {"sessions", "messages", "usage_records"} <= _table_names(tmp_home)


def test_migrate_missing_version_file_treated_as_v1(tmp_home):
    assert migrate(tmp_home / "data") == "2"
    assert _version_file(tmp_home).exists()


def test_migrate_idempotent(tmp_home):
    migrate(tmp_home / "data")
    assert migrate(tmp_home / "data") == "2"  # 已是最新，直接返回
    assert {"sessions", "messages", "usage_records"} <= _table_names(tmp_home)


def test_migrate_failure_aborts(tmp_home, monkeypatch):
    monkeypatch.setitem(MIGRATIONS, "2", MIGRATIONS["2"] + ["THIS IS NOT SQL"])
    with pytest.raises(RuntimeError, match="数据迁移到 schema v2 失败"):
        migrate(tmp_home / "data")
    # 版本文件未更新（不半途宣告成功）
    assert not _version_file(tmp_home).exists()


def test_db_path_layout(tmp_home):
    assert db_path(tmp_home / "data") == tmp_home / "data" / "user" / "neolearn.db"
