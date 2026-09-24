"""schema 迁移：全新安装、逐级升级保数据、幂等、失败中止。"""

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


def _session_columns(tmp_home) -> set[str]:
    conn = sqlite3.connect(db_path(tmp_home / "data"))
    try:
        rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        return {r[1] for r in rows}
    finally:
        conn.close()


def test_fresh_install_creates_v4(tmp_home):
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert _version_file(tmp_home).read_text(encoding="utf-8").strip() == SCHEMA_VERSION
    tables = _table_names(tmp_home)
    assert {"sessions", "messages", "usage_records"} <= tables
    assert "persona_description" in _session_columns(tmp_home)


def test_migrate_v1_to_v4(tmp_home):
    # 模拟 P0 状态：版本文件 v1、无表
    (tmp_home / "data" / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("1\n", encoding="utf-8")
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert {"sessions", "messages", "usage_records"} <= _table_names(tmp_home)
    assert "persona_description" in _session_columns(tmp_home)


def test_migrate_v2_to_latest_preserves_data(tmp_home):
    # 模拟 P1 状态：v2 库含会话行，迁移后数据保留且新列就位
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("2\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statement in MIGRATIONS["2"]:
            conn.execute(statement)
        conn.execute(
            """INSERT INTO sessions
               (id, title, capability, model, persona, language, kb_ids, tool_overrides,
                created_at, updated_at)
               VALUES ('sess-old', '旧会话', 'chat', NULL, 'teacher', 'zh', '[]', '{}', 1.0, 1.0)"""
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert "persona_description" in _session_columns(tmp_home)
    assert "attachments" in _table_names(tmp_home)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT persona FROM sessions WHERE id = 'sess-old'").fetchone()
    finally:
        conn.close()
    assert row == ("teacher",)


def test_migrate_missing_version_file_treated_as_v1(tmp_home):
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert _version_file(tmp_home).exists()


def test_migrate_idempotent(tmp_home):
    migrate(tmp_home / "data")
    assert migrate(tmp_home / "data") == SCHEMA_VERSION  # 已是最新，直接返回
    assert {"sessions", "messages", "usage_records"} <= _table_names(tmp_home)


def test_migrate_failure_aborts(tmp_home, monkeypatch):
    monkeypatch.setitem(MIGRATIONS, "2", MIGRATIONS["2"] + ["THIS IS NOT SQL"])
    with pytest.raises(RuntimeError, match="数据迁移到 schema v2 失败"):
        migrate(tmp_home / "data")
    # 版本文件未更新（不半途宣告成功）
    assert not _version_file(tmp_home).exists()


def test_db_path_layout(tmp_home):
    assert db_path(tmp_home / "data") == tmp_home / "data" / "user" / "neolearn.db"


def test_version_without_migration_definition_aborts(tmp_home, monkeypatch):
    # 事故复现：SCHEMA_VERSION 改大了但 MIGRATIONS 里没有对应条目 →
    # 迁移一条不跑、版本号却被推高（"no such table" 的根因）。必须硬中止。
    monkeypatch.setitem(MIGRATIONS, "5", [])
    monkeypatch.setattr("nnnu.services.sessions.schema.SCHEMA_VERSION", "7")
    with pytest.raises(RuntimeError, match="与 MIGRATIONS 不同步"):
        migrate(tmp_home / "data")
    assert not _version_file(tmp_home).exists()


def test_version_file_ahead_of_db_aborts_with_hint(tmp_home):
    # 事故善后：版本文件已是 v5 而库内容只到 v4（缺 cron_jobs）——
    # 不能带病启动，报错要指出库实际到哪一级、怎么修
    (tmp_home / "data" / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("5\n", encoding="utf-8")
    path = db_path(tmp_home / "data")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="库内容实际只到 v4"):
        migrate(tmp_home / "data")
    # 按提示把版本号改回 v4 → 重跑 v5 迁移补齐
    _version_file(tmp_home).write_text("4\n", encoding="utf-8")
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert "cron_jobs" in _table_names(tmp_home)


def test_db_newer_than_program_aborts(tmp_home):
    (tmp_home / "data" / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("99\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="比本程序"):
        migrate(tmp_home / "data")


def test_fresh_install_creates_notebook_tables(tmp_home):
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert {"notebooks", "notebook_records"} <= _table_names(tmp_home)


def test_migrate_v5_to_v6_keeps_existing_rows(tmp_home):
    # 模拟 P3 状态：v5 库里有会话与定时任务，升到 v6 后两者都还在
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("5\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4", "5"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute(
            """INSERT INTO sessions
               (id, title, capability, model, persona, language, kb_ids, tool_overrides,
                created_at, updated_at)
               VALUES ('sess-v5', '旧会话', 'chat', NULL, NULL, 'zh', '[]', '{}', 1.0, 1.0)"""
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert {"notebooks", "notebook_records"} <= _table_names(tmp_home)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT title FROM sessions WHERE id = 'sess-v5'").fetchone()
    finally:
        conn.close()
    assert row == ("旧会话",)
