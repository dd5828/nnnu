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
SCHEMA_VERSION = "4"

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


def migrate(data_root: Path) -> str:
    """把 data 目录升级到最新 schema，返回新版本号；失败抛 RuntimeError。

    用同步 sqlite3：迁移是启动期一次性操作，先于应用 Database 存在。
    """
    current = _read_version(data_root)
    target = SCHEMA_VERSION
    if current == target:
        return target
    ordered = sorted(MIGRATIONS, key=lambda v: int(v))
    pending = [v for v in ordered if int(v) > int(current)]
    path = db_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        for version in pending:
            try:
                _apply(conn, version)
            except Exception as exc:
                raise RuntimeError(
                    f"数据迁移到 schema v{version} 失败：{exc}。"
                    f"数据文件未损坏，备份路径 {path}，请勿删除后重试"
                ) from exc
        conn.commit()
    finally:
        conn.close()
    # 迁移全部成功后原子更新版本文件
    version_file = _version_file(data_root)
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
