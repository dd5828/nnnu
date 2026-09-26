"""数据 schema 版本与迁移（§8.4）：版本唯一入口，失败硬中止启动。

- data/system/schema_version.txt 记录当前版本；缺失视为 "1"（P0 目录树）；
- 逐级执行 MIGRATIONS 中高于当前版本的迁移（DDL 全部幂等）；
- 任一步失败 raise RuntimeError（提示备份路径，绝不静默丢弃数据）；
- P0 由 bootstrap 写版本文件，自 v2 起责任移交本模块（单写者）。
"""

import sqlite3
import tempfile
from pathlib import Path

# v2：P1 会话/消息/成本表（§8.2 + 偏离：usage_records 为 §6.9 成本汇总新增）
# v3：P2 persona——自定义 persona 描述粘性存储（§7.1）
# v4：P2 附件表（偏离：§8.2 无 attachments 表，§7.1 附件归档/清理需要）
# v5：P3 cron 定时任务表（§8.2 原样）
# v6：P5 批一 笔记本与记录表（§8.2 原样）
# v7：P5 批二 题库——§8.2 questions 原样 + 偏离补四列（题型/知识点/难度/来源会话），
#     另加 question_attempts 作答表（§7.4「保留作答与解析」；§8.2 没有这张表）
# v8：P5 批三 学习路径与知识点树（§7.5；§8.2 无这两张表，记偏离）+
#     questions.node_id 把题挂到节点上（软引用，不挂外键）
# v9：P5 批三**重做**——门就是游标（删 learning_paths.current_node_id 与 advance）、
#     四类两套门（node_type 换 memory/procedure/concept/design + assess_passed/assessed_at）、
#     新增 learning_interactions 答题交互表（§8.2 无此表，记偏离）
SCHEMA_VERSION = "9"

MIGRATIONS: dict[str, list[str]] = {
    "2": [
        """CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            capability TEXT NOT NULL DEFAULT 'chat',
            model TEXT,
            persona TEXT,
            language TEXT DEFAULT 'zh',
            kb_ids TEXT DEFAULT '[]',
            tool_overrides TEXT DEFAULT '{}',
            created_at REAL,
            updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT,
            tool_calls TEXT DEFAULT '[]',
            citations TEXT DEFAULT '[]',
            cost TEXT,
            created_at REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at)",
        """CREATE TABLE IF NOT EXISTS usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            turn_id TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_records(created_at)",
    ],
    "3": [
        "ALTER TABLE sessions ADD COLUMN persona_description TEXT",
    ],
    "4": [
        """CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            mime TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id, created_at)",
    ],
    "5": [
        """CREATE TABLE IF NOT EXISTS cron_jobs (
            id TEXT PRIMARY KEY,
            schedule TEXT NOT NULL,
            prompt TEXT NOT NULL,
            session_id TEXT,
            enabled INTEGER DEFAULT 1,
            last_run_at REAL,
            next_run_at REAL,
            created_at REAL
        )""",
    ],
    "6": [
        """CREATE TABLE IF NOT EXISTS notebooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            created_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS notebook_records (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            content_md TEXT NOT NULL,
            source_ref TEXT,
            created_at REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notebook_records_notebook "
        "ON notebook_records(notebook_id, created_at)",
    ],
    "7": [
        """CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY,
            stem TEXT NOT NULL,
            options TEXT NOT NULL DEFAULT '[]',
            answer TEXT NOT NULL,
            explanation TEXT,
            source TEXT,
            tags TEXT DEFAULT '[]',
            mastery REAL DEFAULT 0,
            wrong_count INTEGER DEFAULT 0,
            last_attempt_at REAL,
            created_at REAL,
            type TEXT NOT NULL DEFAULT 'single',
            knowledge_point TEXT NOT NULL DEFAULT '',
            difficulty TEXT NOT NULL DEFAULT 'medium',
            session_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS question_attempts (
            id TEXT PRIMARY KEY,
            question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            session_id TEXT,
            answer TEXT NOT NULL DEFAULT '',
            correct INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            feedback TEXT,
            source TEXT NOT NULL DEFAULT 'auto',
            created_at REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_questions_kp ON questions(knowledge_point, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_question_attempts_q "
        "ON question_attempts(question_id, created_at)",
    ],
    "8": [
        """CREATE TABLE IF NOT EXISTS learning_paths (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            summary TEXT,
            current_node_id TEXT,
            session_id TEXT,
            created_at REAL,
            updated_at REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_learning_paths_session ON learning_paths(session_id)",
        # sort_order 是「全路径前序连续整数」：下一个节点就是 sort_order + 1，
        # 门控推进、跳过已掌握节点全靠它，改动树结构时统一重排（见 learning/service.py）
        """CREATE TABLE IF NOT EXISTS learning_nodes (
            id TEXT PRIMARY KEY,
            path_id TEXT NOT NULL REFERENCES learning_paths(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES learning_nodes(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT 'concept',
            description TEXT,
            depth INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            mastery REAL NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'not_started',
            review_stage INTEGER NOT NULL DEFAULT 0,
            next_review_at REAL,
            last_practiced_at REAL,
            created_at REAL,
            updated_at REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_learning_nodes_order "
        "ON learning_nodes(path_id, sort_order)",
        "CREATE INDEX IF NOT EXISTS idx_learning_nodes_parent "
        "ON learning_nodes(path_id, parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_learning_nodes_review ON learning_nodes(next_review_at)",
        # 题挂到节点上（软引用，不挂外键：删节点时手动置空，见 service.delete_node）
        "ALTER TABLE questions ADD COLUMN node_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_questions_node ON questions(node_id)",
    ],
    # 注意：本级的 ADD/DROP COLUMN 不是幂等语句（SQLite 没有 IF EXISTS 语法可用），
    # 崩在「DDL 已应用、版本文件还没写」之间要人工把 data/system/schema_version.txt
    # 退回 v8 之前的级别（v8 库退回 8，老库退回实际级别），迁移会重跑补齐。
    "9": [
        # 门就是游标：不再存「当前节点」，每回合从「哪些节点已掌握」现算下一目标
        "ALTER TABLE learning_paths DROP COLUMN current_node_id",
        # 定性门（concept/design）的评定结果：布尔 + 评定时间，与定量门的 mastery 并列
        "ALTER TABLE learning_nodes ADD COLUMN assess_passed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE learning_nodes ADD COLUMN assessed_at REAL",
        # 类型换成四类（旧 calculation/proof 各映射到最近的一个）
        "UPDATE learning_nodes SET node_type = 'procedure' WHERE node_type = 'calculation'",
        "UPDATE learning_nodes SET node_type = 'design' WHERE node_type = 'proof'",
        # 掌握度口径整批作废（题级均值 + 证据封顶 → 作答序列近因加权），不做兼容：
        # 派生列一并清零，否则「掌握度 0 但状态仍写着已掌握」这种自相矛盾的行会留在库里
        "UPDATE learning_nodes SET mastery = 0, state = 'not_started', review_stage = 0, "
        "next_review_at = NULL, last_practiced_at = NULL",
        # 答题交互：判分来源 + 「同一条路径同一时刻只有一张卡在飞」的唯一真源。
        # 不挂外键（本仓约定是软引用）：审计行要活过节点删除，delete_node 不该带走历史。
        """CREATE TABLE IF NOT EXISTS learning_interactions (
            id TEXT PRIMARY KEY,
            path_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            question_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'quiz',
            status TEXT NOT NULL DEFAULT 'awaiting_input',
            card_prompt TEXT NOT NULL DEFAULT '',
            user_answer TEXT NOT NULL DEFAULT '',
            correct INTEGER,
            score REAL,
            feedback TEXT NOT NULL DEFAULT '',
            grade_source TEXT NOT NULL DEFAULT '',
            session_id TEXT,
            turn_id TEXT,
            created_at REAL NOT NULL,
            answered_at REAL,
            updated_at REAL NOT NULL
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_interactions_pending "
        "ON learning_interactions(path_id) WHERE status = 'awaiting_input'",
        "CREATE INDEX IF NOT EXISTS idx_learning_interactions_node "
        "ON learning_interactions(node_id, created_at)",
    ],
}

# 每级迁移应落地的产物——启动自检清单（见 _verify）。
# 曾出过的事故：SCHEMA_VERSION 先于 MIGRATIONS 条目被改大，pending 算成空集，
# 迁移一条没跑、版本文件却写成了新值，之后启动报 "no such table"。自检就是为了
# 让这种"版本文件与库内容对不上"当场硬中止（带可操作的修复提示），而不是带病运行。
EXPECTED_TABLES: dict[str, tuple[str, ...]] = {
    "2": ("sessions", "messages", "usage_records"),
    "4": ("attachments",),
    "5": ("cron_jobs",),
    "6": ("notebooks", "notebook_records"),
    "7": ("questions", "question_attempts"),
    "8": ("learning_paths", "learning_nodes"),
    "9": ("learning_interactions",),
}

EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "3": (("sessions", "persona_description"),),
    "8": (("questions", "node_id"),),
    "9": (("learning_nodes", "assess_passed"), ("learning_nodes", "assessed_at")),
}


def db_path(data_root: Path) -> Path:
    """单文件数据库位置（§8.2 标题：data/user/neolearn.db）。"""
    return data_root / "user" / "neolearn.db"


def _version_file(data_root: Path) -> Path:
    return data_root / "system" / "schema_version.txt"


def _read_version(data_root: Path) -> str:
    version_file = _version_file(data_root)
    if not version_file.exists():
        return "1"  # P0 目录树：无版本文件视为 v1（无任何表）
    return version_file.read_text(encoding="utf-8").strip()


def _apply(conn: sqlite3.Connection, version: str) -> None:
    for statement in MIGRATIONS[version]:
        conn.execute(statement)


def _level_ok(conn: sqlite3.Connection, tables: set[str], level: str) -> bool:
    """该级迁移的产物是否齐备（表 + 列）。"""
    for table in EXPECTED_TABLES.get(level, ()):
        if table not in tables:
            return False
    for table, column in EXPECTED_COLUMNS.get(level, ()):
        if table not in tables:
            return False
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            return False
    return True


def _detected_version(conn: sqlite3.Connection, upto: str) -> str:
    """库内容实际达到的级别：从 upto 往下找第一个产物齐全的级别。"""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for level in sorted(MIGRATIONS, key=int, reverse=True):
        if int(level) <= int(upto) and _level_ok(conn, tables, level):
            return level
    return "1"


def _verify(conn: sqlite3.Connection, version: str, path: Path, version_file: Path) -> None:
    """自检：版本号声称达到 version，则 ≤ version 各级的产物必须都在，否则硬中止。"""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for level in sorted(MIGRATIONS, key=int):
        if int(level) <= int(version) and not _level_ok(conn, tables, level):
            detected = _detected_version(conn, version)
            raise RuntimeError(
                f"数据文件与版本文件对不上：版本号是 v{version}，但 v{level} 该建的产物缺失"
                f"（库内容实际只到 v{detected}）——多半是早先的迁移没真正执行、版本号却被推高了。"
                f"数据文件未损坏，备份路径 {path}；"
                f"把 {version_file} 里的版本号改回 v{detected} 再启动，迁移会重跑补齐"
            )


def migrate(data_root: Path) -> str:
    """把 data 目录升级到最新 schema，返回新版本号；失败抛 RuntimeError。

    用同步 sqlite3：迁移是启动期一次性操作，先于应用 Database 存在。
    """
    current = _read_version(data_root)
    target = SCHEMA_VERSION
    version_file = _version_file(data_root)
    if not current.isdigit():
        raise RuntimeError(f"schema 版本文件内容不是数字：{current!r}（{version_file}）")
    if int(current) > int(target):
        raise RuntimeError(
            f"数据文件 schema v{current} 比本程序（v{target}）新——请升级程序；"
            f"绝不回写版本号降级（会静默丢数据）。备份路径 {db_path(data_root)}"
        )
    # 完整性校验：中间每一级都得有迁移定义，否则版本号会被平白推高而库没跟上
    missing = [v for v in range(int(current) + 1, int(target) + 1) if str(v) not in MIGRATIONS]
    if missing:
        raise RuntimeError(
            f"SCHEMA_VERSION（v{target}）与 MIGRATIONS 不同步：缺少 {missing} 的迁移定义。"
            f"补上迁移或把版本号改回 v{current}，不会动数据文件"
        )
    path = db_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _verify(conn, current, path, version_file)  # 升级前：先确认现状与版本号相符
        for version in (str(v) for v in range(int(current) + 1, int(target) + 1)):
            try:
                _apply(conn, version)
            except Exception as exc:
                raise RuntimeError(
                    f"数据迁移到 schema v{version} 失败：{exc}。"
                    f"数据文件未损坏，备份路径 {path}，请勿删除后重试"
                ) from exc
        _verify(conn, target, path, version_file)  # 升级后：产物齐全才算成功
        conn.commit()
    finally:
        conn.close()
    if current == target:
        return target
    # 迁移全部成功后原子更新版本文件
    version_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=version_file.parent, prefix="schema_version", suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{target}\n")
        Path(tmp_name).replace(version_file)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target
