"""导入第三方 CLI 的历史会话（§7.12）：扫描 → 落盘 → 落库 → 可搜索可引用。

为什么是「服务端直接扫」而不是像上游那样在浏览器里选文件：本地单用户应用，用户的
历史就在这台机器上，多一层上传协议只是多一处能出错的接缝（有意的偏离，登记在案）。

四件容易出事的事，都在这个文件里收口：

1. **扫描根是白名单**。默认只认 `~/.claude/projects`，外加环境变量
   `NNNU_AGENTS_IMPORT_ROOTS` 追加几个（E2E 指向夹具目录）。请求里带的 `root` 必须
   `resolve()` 后落在其中一个根里面——不然 `root="C:/Users/x/.ssh"` 就能把任意目录
   的 `.jsonl` 读出来回吐给调用方。
2. **id 是确定性的**。`imp-<sha256(source::外部 id)[:8]>`（core.ids.stable_id）：
   同一份历史扫第二次落回同一个 id，天然幂等覆盖；digest 记正文指纹，来源那边又聊过
   就整条重导。
3. **id 会进 URL 再进文件系统**。`GET/DELETE /agents/imports/{id}` 的 id 先按正则
   收口、写读删之前再断言路径落在 imports 目录里（双层：正则够用，但将来 id 形状一改
   就只剩第二层挡着）。
4. **正文不灌库**。库里的 `agent_sessions` 只放元数据 + `search_text`（标题与前几条
   消息，约 4000 字上限），整条转录在 `data/user/agents/imports/<id>.jsonl` 里躺着，
   要读时分页读那个文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from nnnu.core.ids import stable_id
from nnnu.services.agents.spec import imports_dir
from nnnu.services.audit import audit_log
from nnnu.services.sessions.db import Database
from nnnu.services.settings.atomic import atomic_write_text

logger = logging.getLogger(__name__)

#: 本批唯一接上的来源（Claude Code 的项目历史）
DEFAULT_SOURCE = "claude_code"
SUPPORTED_SOURCES: tuple[str, ...] = ("claude_code",)
#: 来源 → 界面/提示词里显示的名字（注入上下文时要说清楚这段历史是哪儿来的）
SOURCE_LABELS: dict[str, str] = {"claude_code": "Claude Code"}

#: 追加扫描根的环境变量（os.pathsep 分隔；默认根不会被它顶掉，只做并集）
IMPORT_ROOTS_ENV = "NNNU_AGENTS_IMPORT_ROOTS"

#: id 形状（stable_id 的产出）：进 URL、进文件名，两侧都按它收口
IMPORT_ID_RE = re.compile(r"^imp-[0-9a-f]{8}$")

#: 一次扫描的上限。本机全量（6 个项目目录、几十个文件）远够用，这道闸防的是
#: 有人把根指向一棵异常大的目录树——扫到一半把后端内存吃光比"少导几个"糟得多。
MAX_SCAN_FILES = 500
MAX_IMPORT_BYTES = 32 * 1024 * 1024  # 单个 jsonl 超过它就不导
#: 单条消息正文上限（粘一整份日志进来的那种）
MESSAGE_MAX_CHARS = 4000
MAX_TITLE_CHARS = 80
#: search_text = 标题 + 前若干条消息（不灌整条转录，见 schema.py v16 的注释）
SEARCH_TEXT_MAX_CHARS = 4000
SEARCH_PREVIEW_MESSAGES = 8
#: 列表与转录的分页上限
MAX_PAGE = 200


class AgentImportInvalid(ValueError):
    """扫描根/会话 id 不合法（消息直接给用户看）。"""


class AgentSessionNotFound(ValueError):
    """库里没有这条导入的会话。"""


@dataclass(slots=True)
class ParsedMessage:
    role: str  # user | assistant
    text: str
    ts: float | None = None

    def to_line(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "ts": self.ts}


@dataclass(slots=True)
class ParsedSession:
    """一份历史 jsonl 解析出的可读会话（纯数据，单测直接喂文件）。"""

    source_id: str
    title: str = ""
    cwd: str = ""
    messages: list[ParsedMessage] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    bad_lines: int = 0

    @property
    def digest(self) -> str:
        """正文指纹：标题 + 逐条「角色 + 正文」的哈希（格式噪声不影响它）。"""
        hasher = hashlib.sha256()
        hasher.update(self.title.encode("utf-8"))
        for message in self.messages:
            hasher.update(b"\x1e")
            hasher.update(message.role.encode("utf-8"))
            hasher.update(b"\x1f")
            hasher.update(message.text.encode("utf-8"))
        return hasher.hexdigest()[:16]

    def search_text(self) -> str:
        """检索文本：标题 + 前几条消息（库里的那一份，整条转录在别处）。"""
        parts = [self.title] if self.title else []
        for message in self.messages[:SEARCH_PREVIEW_MESSAGES]:
            parts.append(message.text)
        return "\n".join(parts)[:SEARCH_TEXT_MAX_CHARS]

    def to_payload(self, session_id: str, **extra: Any) -> dict[str, Any]:
        """对外视图（列表/扫描/导入三处共用一份形状）。"""
        data: dict[str, Any] = {
            "id": session_id,
            "source_id": self.source_id,
            "title": self.title,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "message_count": len(self.messages),
        }
        data.update(extra)
        return data


def default_root() -> Path:
    """默认扫描根（惰性取，别在 import 时就把 home 钉死）。"""
    return Path.home() / ".claude" / "projects"


def allowed_roots() -> list[Path]:
    """允许扫描的根：`NNNU_AGENTS_IMPORT_ROOTS` 里的若干个在前 + 默认根兜底。

    顺序有意义：**列表第一个就是「不指定 root」时的默认扫描对象**。设了环境变量的
    场景（E2E 的夹具目录、把历史挪到别处的机器）要的就是扫那里，所以它排在默认根
    前面——不然界面上不填 root 的时候扫的还是 `~/.claude/projects`。
    """
    raw = [
        Path(chunk.strip()).expanduser()
        for chunk in os.environ.get(IMPORT_ROOTS_ENV, "").split(os.pathsep)
        if chunk.strip()
    ]
    raw.append(default_root())
    roots: list[Path] = []
    for item in raw:
        try:
            resolved = item.resolve()
        except OSError:  # 坏路径（超长/无权限）不该让整张白名单失效
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


# ---- 解析（纯函数，可单测） ----


def parse_transcript(path: Path, *, source_id: str = "") -> ParsedSession:
    """读一份 Claude Code 历史 jsonl，抽出可以给人看的正文。

    宽容到底：坏行（写一半、混进别的东西）只计数跳过；`isSidechain`（子代理内部
    往返）与 `isMeta`（系统提示注入）整行不要——那些不是用户与助手的对话。
    只收 user/assistant 的 text block，工具调用压成一行标记（正文里带上下文的
    「助手调用了 Bash」比整块 tool_use JSON 有用得多），thinking 与 tool_result 丢弃。
    """
    parsed = ParsedSession(source_id=source_id or path.stem)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return parsed
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            parsed.bad_lines += 1
            continue
        if not isinstance(obj, dict):
            parsed.bad_lines += 1
            continue
        _harvest_meta(parsed, obj)
        if obj.get("isSidechain") or obj.get("isMeta"):
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        message = _extract_message(obj, parsed)
        if message is not None:
            parsed.messages.append(message)
    if not parsed.title:
        # 没有 aiTitle（老版本没写过）就退回首条用户消息——列表里总得有个能认人的名字
        first = next((item for item in parsed.messages if item.role == "user"), None)
        if first is not None:
            parsed.title = " ".join(first.text.split())[:MAX_TITLE_CHARS]
    return parsed


def _harvest_meta(parsed: ParsedSession, obj: dict[str, Any]) -> None:
    """从任意一行顺手捡元信息（这些字段散落在不同事件的行上）。"""
    title = obj.get("aiTitle")
    if isinstance(title, str) and title.strip():
        parsed.title = title.strip()[:MAX_TITLE_CHARS]  # 后写的标题更准，覆盖
    cwd = obj.get("cwd")
    if not parsed.cwd and isinstance(cwd, str) and cwd.strip():
        parsed.cwd = cwd.strip()
    stamp = _to_epoch(obj.get("timestamp"))
    if stamp is not None:
        parsed.started_at = stamp if parsed.started_at is None else min(parsed.started_at, stamp)
        parsed.ended_at = stamp if parsed.ended_at is None else max(parsed.ended_at, stamp)


def _extract_message(obj: dict[str, Any], parsed: ParsedSession) -> ParsedMessage | None:
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role") or obj.get("type")
    if role not in ("user", "assistant"):
        return None
    text, tools = _read_content(message.get("content"))
    if tools:
        # 工具调用压成一行标记：读的人知道「这里它动过手」就够了
        marker = "（调用了工具：" + "、".join(tools) + "）"
        text = f"{text}\n{marker}" if text else marker
    text = text.strip()
    if not text:
        return None
    return ParsedMessage(
        role=role, text=text[:MESSAGE_MAX_CHARS], ts=_to_epoch(obj.get("timestamp"))
    )


def _read_content(content: Any) -> tuple[str, list[str]]:
    """content 可能是字符串或块数组；返回（正文, 工具名清单）。"""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    texts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                tools.append(name)
    return "\n".join(texts), tools


def _to_epoch(value: Any) -> float | None:
    """ISO 时间字符串 → epoch 秒（坏值一律当没有，不抛）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ---- 扫描目录（子线程里跑） ----


@dataclass(slots=True)
class _FileItem:
    path: Path
    project: str
    parsed: ParsedSession


@dataclass(slots=True)
class _CollectResult:
    items: list[_FileItem] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False


def _collect(root: Path, cutoff: float | None) -> _CollectResult:
    """遍历根下的 `.jsonl` 并解析（跑在子线程：解析几十 MB 不该堵事件循环）。

    `cutoff` 是「只看最近 N 天」的时间线，先拿文件 mtime 粗筛（一次 stat 的事），
    免得为了筛掉几天前的会话去把每个文件都解析一遍。
    """
    result = _CollectResult()
    if not root.is_dir():
        result.skipped.append({"path": str(root), "reason": "目录不存在或不是目录"})
        return result
    files = sorted(root.rglob("*.jsonl"))
    if len(files) > MAX_SCAN_FILES:
        result.truncated = True
        files = files[:MAX_SCAN_FILES]
    seen: set[str] = set()
    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            result.skipped.append({"path": str(path), "reason": f"读不了：{exc}"})
            continue
        if stat.st_size > MAX_IMPORT_BYTES:
            result.skipped.append(
                {"path": str(path), "reason": f"文件超过 {MAX_IMPORT_BYTES // 1024 // 1024}MB"}
            )
            continue
        if cutoff is not None and stat.st_mtime < cutoff:
            continue
        parsed = parse_transcript(path)
        if parsed.source_id in seen:  # 两个根重叠时同一份历史只算一条
            continue
        if not parsed.messages:
            result.skipped.append({"path": str(path), "reason": "没有可读的对话正文"})
            continue
        seen.add(parsed.source_id)
        result.items.append(_FileItem(path=path, project=path.parent.name, parsed=parsed))
    return result


# ---- 服务 ----


class AgentImportService:
    """导入会话的单一入口（路由与引用解析都走它）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 扫描 / 导入 ----

    async def scan(self, *, root: str = "", days: float = 0.0) -> dict[str, Any]:
        """列候选：扫一遍根，和库里已有的对一遍（已导入/有变化 都如实标出来）。"""
        base = resolve_root(root)
        collected = await asyncio.to_thread(_collect, base, _cutoff(days))
        known = await self._known()
        sessions: list[dict[str, Any]] = []
        for item in collected.items:
            session_id = _session_id(item.parsed)
            row = known.get(session_id)
            sessions.append(
                item.parsed.to_payload(
                    session_id,
                    source=DEFAULT_SOURCE,
                    project=item.project,
                    path=str(item.path),
                    imported=row is not None,
                    changed=row is not None and row.get("digest") != item.parsed.digest,
                    imported_at=row.get("imported_at") if row else None,
                )
            )
        sessions.sort(
            key=lambda entry: entry.get("ended_at") or entry.get("started_at") or 0, reverse=True
        )
        # 最坏的方向：把根指向一棵大树。截断要如实说，不然用户以为"就这些"
        if len(sessions) > MAX_PAGE:
            collected.truncated = True
            sessions = sessions[:MAX_PAGE]
        return {
            "root": str(base),
            "roots": [str(item) for item in allowed_roots()],
            "sessions": sessions,
            "skipped": collected.skipped,
            "truncated": collected.truncated,
        }

    async def import_sessions(
        self, *, root: str = "", days: float = 0.0, ids: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """扫 + 导。`ids` 为空 = 导入扫到的全部；给了 id = 只导这几条（用户勾的那几条）。

        幂等靠 id + digest：同一份历史再扫一次，没变的直接算「已是最新」，变了的整条
        覆盖重导。显式点名的那些即使没变也重写一遍——用户就是来点它的，顺手补上
        正文文件缺失之类的半个状态。
        """
        base = resolve_root(root)
        collected = await asyncio.to_thread(_collect, base, _cutoff(days))
        wanted = {str(item) for item in ids} if ids else None
        known = await self._known()
        imported: list[dict[str, Any]] = []
        unchanged: list[str] = []
        missing = set(wanted or ())
        for item in collected.items:
            session_id = _session_id(item.parsed)
            if wanted is not None and session_id not in wanted:
                continue
            missing.discard(session_id)
            parsed = item.parsed
            row = known.get(session_id)
            if (
                row is not None
                and row.get("digest") == parsed.digest
                and transcript_path(session_id).is_file()
                and wanted is None
            ):
                unchanged.append(session_id)
                continue
            await asyncio.to_thread(_write_transcript, session_id, parsed)
            await self._upsert(session_id, parsed)
            imported.append(
                parsed.to_payload(
                    session_id,
                    source=DEFAULT_SOURCE,
                    project=item.project,
                    path=str(item.path),
                    imported=True,
                    changed=row is None or row.get("digest") != parsed.digest,
                )
            )
        errors = [
            {"id": missing_id, "reason": "这一轮没扫到（历史被删了？或不在所选天数内）"}
            for missing_id in sorted(missing)
        ]
        if imported:
            audit_log(
                "agent_session_import",
                root=str(base),
                count=len(imported),
                ids=[entry["id"] for entry in imported][:50],
            )
        return {
            "root": str(base),
            "imported": imported,
            "unchanged": unchanged,
            "errors": errors,
            "skipped": collected.skipped,
            "truncated": collected.truncated,
        }

    # ---- 查 / 读 / 删 ----

    async def list_sessions(
        self, *, q: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """已导入列表（标题 / 正文前段搜）。`%`、`_` 按字面匹配（ESCAPE），别当通配符。"""
        size = _clamp(limit, 50, MAX_PAGE)
        start = max(int(offset or 0), 0)
        where, params = "", []
        text = (q or "").strip()[:200]
        if text:
            pattern = f"%{_escape_like(text)}%"
            where = "WHERE title LIKE ? ESCAPE '\\' OR search_text LIKE ? ESCAPE '\\'"
            params = [pattern, pattern]
        total_row = await self._db.fetch_one(
            f"SELECT COUNT(*) AS n FROM agent_sessions {where}", tuple(params)
        )
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM agent_sessions {where} "
            "ORDER BY ended_at DESC, imported_at DESC, id LIMIT ? OFFSET ?",
            (*params, size, start),
        )
        return {
            "sessions": [_row_payload(row) for row in rows],
            "total": int(total_row["n"]) if total_row else 0,
            "limit": size,
            "offset": start,
        }

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """一条已导入会话的元数据（id 形状不对 → 直接当不存在，不去查库）。"""
        if not _valid_id(session_id):
            return None
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM agent_sessions WHERE id = ?", (session_id,)
        )
        return _row_payload(row) if row is not None else None

    async def read_transcript(
        self, session_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """分页读正文（先查库：id 权威来自库里那条记录，不是 URL 上那个字符串）。"""
        session = await self.get_session(session_id)
        if session is None:
            raise AgentSessionNotFound(f"没有这条导入的会话：{session_id}")
        size = _clamp(limit, 50, MAX_PAGE)
        start = max(int(offset or 0), 0)
        messages, total = await asyncio.to_thread(
            _read_messages, transcript_path(session_id), start, size
        )
        return {
            "session": session,
            "messages": messages,
            "total": total,
            "limit": size,
            "offset": start,
            "has_more": start + len(messages) < total,
        }

    async def delete(self, session_id: str) -> dict[str, Any]:
        """删一条：先查库拿权威 id，再删正文文件，最后删行（库是索引，文件是正文）。"""
        session = await self.get_session(session_id)
        if session is None:
            raise AgentSessionNotFound(f"没有这条导入的会话：{session_id}")
        path = transcript_path(session_id)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
        await self._db.execute("DELETE FROM agent_sessions WHERE id = ?", (session_id,))
        audit_log("agent_session_delete", id=session_id, title=session.get("title", ""))
        return {"deleted": session_id}

    # ---- 内部 ----

    async def _known(self) -> dict[str, dict[str, Any]]:
        rows = await self._db.fetch_all("SELECT id, digest, imported_at FROM agent_sessions")
        return {row["id"]: row for row in rows}

    async def _upsert(self, session_id: str, parsed: ParsedSession) -> None:
        await self._db.execute(
            """
            INSERT INTO agent_sessions
                (id, source, source_id, title, cwd, started_at, ended_at,
                 message_count, digest, imported_at, search_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source = excluded.source,
                source_id = excluded.source_id,
                title = excluded.title,
                cwd = excluded.cwd,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                message_count = excluded.message_count,
                digest = excluded.digest,
                imported_at = excluded.imported_at,
                search_text = excluded.search_text
            """,
            (
                session_id,
                DEFAULT_SOURCE,
                parsed.source_id,
                parsed.title,
                parsed.cwd,
                parsed.started_at,
                parsed.ended_at,
                len(parsed.messages),
                parsed.digest,
                _now(),
                parsed.search_text(),
            ),
        )


# ---- 模块级工具 ----


_COLUMNS = (
    "id, source, source_id, title, cwd, started_at, ended_at, message_count, digest, "
    "imported_at, search_text"
)


def resolve_root(root: str) -> Path:
    """请求里的 root → 允许扫描的实际目录（不在白名单里 → 抛，消息给用户看）。

    空串 = 默认根。白名单是**包含**关系而不是相等：用户可能想看某个项目子目录
    （`~/.claude/projects/xxx`），那也算在允许范围内。
    """
    roots = allowed_roots()
    if not (root or "").strip():
        if not roots:
            raise AgentImportInvalid("没有可用的历史会话目录")
        return roots[0]
    candidate = Path(root.strip()).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise AgentImportInvalid(f"目录不可用：{candidate}（{exc}）") from exc
    for base in roots:
        if resolved == base or resolved.is_relative_to(base):
            return resolved
    allowed = "、".join(str(item) for item in roots)
    raise AgentImportInvalid(f"只允许扫描这些目录：{allowed}")


def transcript_path(session_id: str) -> Path:
    """`<imports 目录>/<id>.jsonl`；id 形状不对或算出来的路径跑出目录 → 抛。

    第二层断言看着多余（正则已经把 id 焊死了），但 id 的生成规则将来一动，这里就是
    最后一道闸——一个能穿越的路径参数配一次 `unlink()` 就是删用户的文件。
    """
    _require_id(session_id)
    directory = imports_dir().resolve()
    path = (directory / f"{session_id}.jsonl").resolve()
    if path.parent != directory:
        raise AgentImportInvalid(f"会话 id 越界：{session_id}")
    return path


def _session_id(parsed: ParsedSession) -> str:
    """确定性 id：来源 + 外部 id 的哈希（重复导入幂等，见模块头第 2 条）。"""
    return stable_id("imp", f"{DEFAULT_SOURCE}::{parsed.source_id}")


def _valid_id(session_id: str) -> bool:
    return bool(IMPORT_ID_RE.match(session_id or ""))


def _require_id(session_id: str) -> None:
    if not _valid_id(session_id):
        raise AgentImportInvalid(f"会话 id 形状不对：{session_id!r}")


def _write_transcript(session_id: str, parsed: ParsedSession) -> None:
    """正文落盘（原子写）：一行一条消息，读的时候好分页。"""
    lines = [json.dumps(message.to_line(), ensure_ascii=False) for message in parsed.messages]
    atomic_write_text(transcript_path(session_id), "\n".join(lines) + "\n")


def _read_messages(path: Path, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    """读转录的一页（坏行跳过，不因为一行坏了整页读不出来）。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    messages: list[dict[str, Any]] = []
    total = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if total >= offset and len(messages) < limit:
            messages.append(
                {
                    "role": str(entry.get("role") or ""),
                    "text": str(entry.get("text") or ""),
                    "ts": entry.get("ts"),
                }
            )
        total += 1
    return messages, total


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    """库里的一行 → 对外视图（search_text 不外发：几千字的检索文本前端用不上）。"""
    return {
        "id": row.get("id", ""),
        "source": row.get("source", ""),
        "source_id": row.get("source_id", ""),
        "title": row.get("title", ""),
        "cwd": row.get("cwd", ""),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "message_count": int(row.get("message_count") or 0),
        "imported_at": row.get("imported_at"),
    }


def _cutoff(days: float) -> float | None:
    """「最近 N 天」的时间线；<=0 或坏值 = 不限。"""
    try:
        value = float(days)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return _now() - value * 86400.0


def _now() -> float:
    return time.time()


def _clamp(value: int, default: int, top: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, top)


def _escape_like(text: str) -> str:
    """LIKE 的转义：`\\`、`%`、`_` 按字面匹配（否则搜「50%」等于搜「50 加任意东西」）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- 单例（装配照 skills：api/main.py 的 lifespan 里建，路由与引用解析都从这里取） ----

_service: AgentImportService | None = None


def set_agent_import_service(service: AgentImportService | None) -> None:
    global _service
    _service = service


def get_agent_import_service() -> AgentImportService:
    if _service is None:
        raise RuntimeError("子代理导入服务未装配（先 set_agent_import_service）")
    return _service
