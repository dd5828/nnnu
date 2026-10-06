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
    monkeypatch.setattr(
        "nnnu.services.sessions.schema.SCHEMA_VERSION", str(int(SCHEMA_VERSION) + 1)
    )
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


def _columns(tmp_home, table: str) -> set[str]:
    conn = sqlite3.connect(db_path(tmp_home / "data"))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_fresh_install_creates_question_tables(tmp_home):
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert {"questions", "question_attempts"} <= _table_names(tmp_home)
    # §8.2 的四列是偏离补的：题型/知识点/难度/来源会话
    assert {"type", "knowledge_point", "difficulty", "session_id"} <= _columns(
        tmp_home, "questions"
    )
    assert {"score", "correct", "feedback", "source"} <= _columns(tmp_home, "question_attempts")


def test_migrate_v6_to_v7_keeps_existing_rows(tmp_home):
    # 模拟 P5 批一状态：v6 库里有会话与笔记本，升到 v7 后两者都还在、新表就位
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("6\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4", "5", "6"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute("INSERT INTO notebooks (id, name, description) VALUES ('nb-v6', '旧本', NULL)")
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert {"questions", "question_attempts"} <= _table_names(tmp_home)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT name FROM notebooks WHERE id = 'nb-v6'").fetchone() == ("旧本",)
        # 建表时默认值就位：老行（如果有）不需要回填也能读
        conn.execute("INSERT INTO questions (id, stem, answer) VALUES ('q-v7', '题面', 'A')")
        row = conn.execute(
            "SELECT type, knowledge_point, difficulty, mastery FROM questions WHERE id = 'q-v7'"
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    assert row == ("single", "", "medium", 0.0)


def test_fresh_install_creates_learning_tables(tmp_home):
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert {"learning_paths", "learning_nodes"} <= _table_names(tmp_home)
    # 树结构 + 门控/复习所需的列（§7.5 自定数值都落在这些列上）
    assert {
        "parent_id",
        "depth",
        "sort_order",
        "mastery",
        "state",
        "review_stage",
        "next_review_at",
    } <= _columns(tmp_home, "learning_nodes")
    # 题目软引用节点（不加强外键：节点删了题还在，只是 node_id 置空）
    assert "node_id" in _columns(tmp_home, "questions")


def test_migrate_v7_to_v8_keeps_rows(tmp_home):
    # 模拟批二状态：v7 库里有题（可能已有作答），升到 v8 后题目与作答都还在，
    # 新列 node_id 对老数据是 NULL（还没挂到任何节点上）
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("7\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4", "5", "6", "7"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO questions (id, stem, answer, mastery, wrong_count) "
            "VALUES ('q-v7', '批二的题', 'A', 0.5, 1)"
        )
        conn.execute(
            "INSERT INTO question_attempts (id, question_id, answer, correct, score, source) "
            "VALUES ('att-v7', 'q-v7', 'A', 0, 0.0, 'deterministic')"
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert {"learning_paths", "learning_nodes"} <= _table_names(tmp_home)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT stem, mastery, node_id FROM questions WHERE id = 'q-v7'"
        ).fetchone()
        attempts = conn.execute("SELECT COUNT(*) FROM question_attempts").fetchone()
    finally:
        conn.close()
    assert row == ("批二的题", 0.5, None)
    assert attempts == (1,)


def test_migrate_v8_idempotent(tmp_home):
    migrate(tmp_home / "data")
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert {"learning_paths", "learning_nodes"} <= _table_names(tmp_home)
    assert "node_id" in _columns(tmp_home, "questions")


def test_migrate_v9_to_v10_keeps_rows(tmp_home):
    # 模拟批三状态：v9 库里有路径/节点/答题交互与一道题，升到 v10 后全都在，
    # research_runs 就位、且「一路会话只有一份在飞的调研」是条**部分唯一**索引
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("9\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4", "5", "6", "7", "8", "9"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO learning_paths (id, topic, title, session_id, created_at, updated_at) "
            "VALUES ('lpath-v9', '线代', '线代', 'sess-v9', 1.0, 2.0)"
        )
        conn.execute(
            "INSERT INTO learning_nodes (id, path_id, parent_id, title, node_type, description, "
            "depth, sort_order, mastery, state) "
            "VALUES ('lnode-v9', 'lpath-v9', NULL, '向量', 'concept', NULL, 0, 0, 0.0, 'not_started')"
        )
        conn.execute(
            "INSERT INTO learning_interactions (id, path_id, node_id, created_at, updated_at) "
            "VALUES ('lint-v9', 'lpath-v9', 'lnode-v9', 1.0, 1.0)"
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert "research_runs" in _table_names(tmp_home)
    conn = sqlite3.connect(path)
    try:
        node = conn.execute("SELECT title, node_type FROM learning_nodes").fetchone()
        interaction = conn.execute("SELECT status FROM learning_interactions").fetchone()
        # 新表默认值就位：两段式回合里「在等确认」的那条行直接 INSERT 就能读
        conn.execute(
            "INSERT INTO research_runs (id, session_id, created_at, updated_at) "
            "VALUES ('rrun-v10', 'sess-v9', 1.0, 1.0)"
        )
        run = conn.execute(
            "SELECT mode, depth, subtopics, status, failed_subtopics FROM research_runs"
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    assert node == ("向量", "concept")
    assert interaction == ("awaiting_input",)
    assert run == ("report", "standard", "[]", "confirming", "[]")

    indexes = _indexes(tmp_home, "research_runs")
    assert indexes["idx_research_runs_active"] == (1, 1)  # 唯一 + 部分（只拦在飞行）


def test_migrate_v10_to_v11_keeps_rows(tmp_home):
    # 模拟 P6 状态：v10 库里有道老题（还没有笔记/错因/来源列）。升到 v11 后题还在、
    # 四列就位：老行按默认值读（note NULL、error_causes 空数组文本、parent_id NULL），
    # 新行能用新列写进去，来源索引是普通索引（变式题不唯一）。
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("10\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4", "5", "6", "7", "8", "9", "10"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO questions (id, stem, options, answer, tags, type, knowledge_point, "
            "difficulty, created_at) "
            "VALUES ('q-v10', '老题面', '[]', '老答案', '[]', 'short', '老知识点', 'medium', 1.0)"
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert {"note", "note_updated_at", "error_causes", "parent_id"} <= _columns(
        tmp_home, "questions"
    )
    conn = sqlite3.connect(path)
    try:
        old_row = conn.execute(
            "SELECT stem, note, note_updated_at, error_causes, parent_id FROM questions "
            "WHERE id = 'q-v10'"
        ).fetchone()
        conn.execute(
            "INSERT INTO questions (id, stem, options, answer, tags, type, knowledge_point, "
            "difficulty, created_at, note, note_updated_at, error_causes, parent_id) "
            "VALUES ('q-v11', '新题面', '[]', '新答案', '[]', 'short', '新知识点', 'medium', 2.0, "
            "'笔记正文', 3.0, '[\"calculation\"]', 'q-v10')"
        )
        new_row = conn.execute(
            "SELECT note, note_updated_at, error_causes, parent_id FROM questions "
            "WHERE id = 'q-v11'"
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    assert old_row == ("老题面", None, None, "[]", None)
    assert new_row == ("笔记正文", 3.0, '["calculation"]', "q-v10")


def test_migrate_v11_to_v12_keeps_rows(tmp_home):
    # 模拟 P9 题库增强后的状态：v11 库里有会话与消息（还没有 metadata 列）。升到 v12 后
    # 消息还在、metadata 列就位：老行 NULL（读取端兜底空 dict），新行能写引用快照。
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("11\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in (str(v) for v in range(2, 12)):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) "
            "VALUES ('s-v11', '老会话', 1.0, 1.0)"
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES ('m-v11', 's-v11', 'user', '老消息', 1.0)"
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert "metadata" in _columns(tmp_home, "messages")
    conn = sqlite3.connect(path)
    try:
        old_row = conn.execute(
            "SELECT content, metadata FROM messages WHERE id = 'm-v11'"
        ).fetchone()
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, metadata, created_at) "
            "VALUES ('m-v12', 's-v11', 'user', '新消息', '{\"refs\": []}', 2.0)"
        )
        new_row = conn.execute("SELECT metadata FROM messages WHERE id = 'm-v12'").fetchone()
        conn.commit()
    finally:
        conn.close()
    assert old_row == ("老消息", None)
    assert new_row == ('{"refs": []}',)
    # 来源索引是普通索引（非唯一、非部分：道道变式题都挂同一道源题是常态）
    assert _indexes(tmp_home, "questions")["idx_questions_parent"] == (0, 0)


def test_migrate_v9_idempotent(tmp_home):
    migrate(tmp_home / "data")
    assert migrate(tmp_home / "data") == SCHEMA_VERSION
    assert {"learning_paths", "learning_nodes"} <= _table_names(tmp_home)
    assert "node_id" in _columns(tmp_home, "questions")
    # v10 与 v9 一样是「建表 + 部分唯一索引」，重跑不该把索引丢掉
    assert _indexes(tmp_home, "research_runs")["idx_research_runs_active"] == (1, 1)


def test_research_runs_allows_one_active_per_session(tmp_home):
    # 部分唯一索引本身的行为（不走服务层）：第二条在飞行撞索引，终态行想加几条加几条
    migrate(tmp_home / "data")
    conn = sqlite3.connect(db_path(tmp_home / "data"))
    try:
        insert = (
            "INSERT INTO research_runs (id, session_id, status, created_at, updated_at) "
            "VALUES (?, 'sess-1', ?, 1.0, 1.0)"
        )
        conn.execute(insert, ("rrun-1", "confirming"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("rrun-2", "researching"))
        conn.execute(insert, ("rrun-3", "reported"))
        # 另一条会话不受影响
        conn.execute(
            "INSERT INTO research_runs (id, session_id, status, created_at, updated_at) "
            "VALUES ('rrun-4', 'sess-2', 'confirming', 1.0, 1.0)"
        )
        conn.commit()
    finally:
        conn.close()


def _indexes(tmp_home, table: str) -> dict[str, tuple[int, int]]:
    """PRAGMA index_list 的（unique, partial）两列——本级的索引是否「部分唯一」就看它。"""
    conn = sqlite3.connect(db_path(tmp_home / "data"))
    try:
        return {
            row[1]: (int(row[2]), int(row[4]))
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
    finally:
        conn.close()


def test_migrate_v8_to_v9_keeps_rows(tmp_home):
    # 模拟批二状态：v8 库里有路径（带 current_node_id 游标）、两个旧类型节点、挂节点上的题。
    # 升到 v9 后：旧类型映射成 procedure/design、游标列消失（门就是游标，库里不再存当前节点）、
    # 掌握度口径整批作废，learning_interactions 与「一条路径同时只有一张卡在飞」的部分唯一索引就位。
    data = tmp_home / "data"
    (data / "system").mkdir(parents=True)
    _version_file(tmp_home).write_text("8\n", encoding="utf-8")
    path = db_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for level in ("2", "3", "4", "5", "6", "7", "8"):
            for statement in MIGRATIONS[level]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO learning_paths (id, topic, title, summary, current_node_id, session_id, "
            "created_at, updated_at) "
            "VALUES ('lpath-v8', '线代', '线代', NULL, 'lnode-calc', 'sess-v8', 1.0, 2.0)"
        )
        conn.executemany(
            "INSERT INTO learning_nodes (id, path_id, parent_id, title, node_type, description, "
            "depth, sort_order, mastery, state, review_stage, next_review_at, last_practiced_at, "
            "created_at, updated_at) "
            "VALUES (?, 'lpath-v8', NULL, ?, ?, NULL, 0, ?, 0.8, 'reviewing', 2, 111.0, 99.0, 1.0, 2.0)",
            [
                ("lnode-calc", "计算与化简", "calculation", 0),
                ("lnode-proof", "证明与推导", "proof", 1),
            ],
        )
        conn.execute(
            "INSERT INTO questions (id, stem, answer, mastery, wrong_count, node_id) "
            "VALUES ('q-v8', '批二的题', 'A', 0.5, 1, 'lnode-calc')"
        )
        conn.commit()
    finally:
        conn.close()

    assert migrate(data) == SCHEMA_VERSION
    assert "learning_interactions" in _table_names(tmp_home)
    assert "current_node_id" not in _columns(tmp_home, "learning_paths")
    assert {"assess_passed", "assessed_at"} <= _columns(tmp_home, "learning_nodes")
    conn = sqlite3.connect(path)
    try:
        types = dict(conn.execute("SELECT id, node_type FROM learning_nodes ORDER BY sort_order"))
        # 旧口径的派生列一并清零：留着会写出「掌握度 0 却写着已掌握」这种自相矛盾的行
        stale = conn.execute(
            "SELECT mastery, state, review_stage, next_review_at, last_practiced_at "
            "FROM learning_nodes WHERE id = 'lnode-calc'"
        ).fetchone()
        question = conn.execute("SELECT stem, node_id FROM questions WHERE id = 'q-v8'").fetchone()
        session = conn.execute(
            "SELECT session_id FROM learning_paths WHERE id = 'lpath-v8'"
        ).fetchone()
    finally:
        conn.close()
    assert types == {"lnode-calc": "procedure", "lnode-proof": "design"}
    assert stale == (0.0, "not_started", 0, None, None)
    assert question == ("批二的题", "lnode-calc")  # 题目与其软引用不受迁移影响
    assert session == ("sess-v8",)

    indexes = _indexes(tmp_home, "learning_interactions")
    assert indexes["idx_learning_interactions_pending"] == (1, 1)  # 唯一 + 部分（只拦未决行）
    assert indexes["idx_learning_interactions_node"] == (0, 0)
