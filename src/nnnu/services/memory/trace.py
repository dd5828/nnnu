"""L1 事件轨迹（§7.10 / §8.3）：按月分片的 append-only JSONL。

- 顶层五键严格等于 §8.3：ts / surface / session_id / event / data；turn_id 与
  seq 收在 data 里（引用格式 `[L1:chat/2026-09.jsonl#123]` 的 123 就是行号）。
- seq = 该行在文件里的 1-based 行号，写者钉死；resolve_ref 回读校验
  （seq 与行号不符 = 带外编辑/漂移，判引用失效——audit 标 STALE，图谱标 ref_broken）。
- 单行 ≤ 4KB：fit_line 按 **UTF-8 字节** 构造保证（中文一字三字节，不能按字数算）。
- rows_from_events 是纯函数（bus.history → 待写行）；IO 全在 TraceWriter 里，
  写失败只 log——轨迹永远不许把回合拖挂（编排器侧还有第二道兜底）。

偏离记录：§8.3 说「按天分片」，但 §7.10/§8.1 与引用示例都是按月——按月。
"""

import asyncio
import copy
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from nnnu.core.events import StreamEvent, StreamEventType
from nnnu.services.memory import paths

logger = logging.getLogger(__name__)

MAX_LINE_BYTES = 4096
MAX_USER_CHARS = 1000
MAX_ASSISTANT_CHARS = 1500
MAX_SUMMARY_CHARS = 300
MAX_ARGS_CHARS = 200
MAX_QA_CHARS = 300
MAX_PER_MODEL = 5

_SURFACE_RE = re.compile(r"^[a-z0-9_]+$")
_FILE_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_REF_RE = re.compile(r"^(?P<surface>[a-z0-9_]+)/(?P<file>\d{4}-\d{2}\.jsonl)#(?P<line>[1-9]\d*)$")

# fit_line 超限时按序减半的字段（正文类；结构字段不缩——缩了引用就对不上）
_SHRINK_KEYS = ("text", "summary", "args_preview", "question", "answer")


def _cap(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断（字段上限）；返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _row(
    surface: str, session_id: str | None, ts: float, event: str, data: dict[str, Any]
) -> dict[str, Any]:
    return {"ts": ts, "surface": surface, "session_id": session_id, "event": event, "data": data}


def user_message_row(
    *, surface: str, session_id: str | None, turn_id: str, text: str
) -> dict[str, Any]:
    """begin_turn 的用户消息行（唯一不在 bus.history 里的 L1 事件）。"""
    capped, truncated = _cap(text, MAX_USER_CHARS)
    return _row(
        surface,
        session_id,
        time.time(),
        "user_message",
        {"turn_id": turn_id, "text": capped, "chars": len(text), "truncated": truncated},
    )


def rows_from_events(
    events: list[StreamEvent],
    *,
    turn_id: str,
    surface: str,
    session_id: str | None,
) -> list[dict[str, Any]]:
    """bus.history → L1 行（不含 seq，写时钉）。纯函数。

    映射：tool_call（与 tool_result 按 call_id 配对回填 ok/summary）、ask_user
    （与 ask_user_reply 按 ask_id 配对）、assistant_done（done.response，缺则
    content_done.full_text；stopped 无 done 记 stopped；error 收尾记 failed）、
    cost（per_model 按 cost 取前 5，其余并成 others）。
    """
    results: dict[str, dict[str, Any]] = {}
    replies: dict[str, str] = {}
    last_content = ""
    for event in events:
        payload = event.payload
        if event.type == StreamEventType.TOOL_RESULT:
            results[str(payload.get("call_id", ""))] = payload
        elif event.type == StreamEventType.ASK_USER_REPLY:
            replies[str(payload.get("ask_id", ""))] = str(payload.get("answer", ""))
        elif event.type == StreamEventType.CONTENT_DONE:
            last_content = str(payload.get("full_text", ""))
    types = {event.type for event in events}

    rows: list[dict[str, Any]] = []
    for event in events:
        payload = event.payload
        if event.type == StreamEventType.TOOL_CALL:
            result = results.get(str(payload.get("call_id", "")))
            try:
                args_text = json.dumps(payload.get("args") or {}, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                args_text = str(payload.get("args"))
            args_preview, cut_args = _cap(args_text, MAX_ARGS_CHARS)
            summary, cut_summary = _cap(str((result or {}).get("summary", "")), MAX_SUMMARY_CHARS)
            rows.append(
                _row(
                    surface,
                    session_id,
                    event.ts,
                    "tool_call",
                    {
                        "turn_id": turn_id,
                        "tool_name": str(payload.get("tool_name", "")),
                        "call_id": str(payload.get("call_id", "")),
                        "ok": None if result is None else bool(result.get("ok")),
                        "summary": summary,
                        "args_preview": args_preview,
                        "truncated": cut_args or cut_summary,
                    },
                )
            )
        elif event.type == StreamEventType.ASK_USER:
            answer = replies.get(str(payload.get("ask_id", "")))
            question, cut_q = _cap(str(payload.get("question", "")), MAX_QA_CHARS)
            answer_text, cut_a = _cap(answer or "", MAX_QA_CHARS)
            rows.append(
                _row(
                    surface,
                    session_id,
                    event.ts,
                    "ask_user",
                    {
                        "turn_id": turn_id,
                        "question": question,
                        "options": [str(o.get("label", "")) for o in payload.get("options") or []],
                        "answered": answer is not None,
                        "answer": answer_text,
                        "truncated": cut_q or cut_a,
                    },
                )
            )
        elif event.type == StreamEventType.DONE:
            response = str(payload.get("response", ""))
            text, truncated = _cap(response or last_content, MAX_ASSISTANT_CHARS)
            rows.append(
                _row(
                    surface,
                    session_id,
                    event.ts,
                    "assistant_done",
                    {
                        "turn_id": turn_id,
                        "status": str(payload.get("status", "completed")),
                        "text": text,
                        "chars": len(response),
                        "truncated": truncated,
                    },
                )
            )
        elif event.type == StreamEventType.STOPPED and StreamEventType.DONE not in types:
            text, truncated = _cap(last_content, MAX_ASSISTANT_CHARS)
            rows.append(
                _row(
                    surface,
                    session_id,
                    event.ts,
                    "assistant_done",
                    {
                        "turn_id": turn_id,
                        "status": "stopped",
                        "text": text,
                        "chars": len(last_content),
                        "truncated": truncated,
                    },
                )
            )
        elif (
            event.type == StreamEventType.ERROR
            and StreamEventType.DONE not in types
            and StreamEventType.STOPPED not in types
        ):
            text, truncated = _cap(last_content, MAX_ASSISTANT_CHARS)
            rows.append(
                _row(
                    surface,
                    session_id,
                    event.ts,
                    "assistant_done",
                    {
                        "turn_id": turn_id,
                        "status": "failed",
                        "text": text,
                        "chars": len(last_content),
                        "truncated": truncated,
                    },
                )
            )
        elif event.type == StreamEventType.COST_SUMMARY:
            per_model = payload.get("per_model") or {}
            ranked = sorted(
                per_model.items(),
                key=lambda kv: float((kv[1] or {}).get("cost") or 0.0),
                reverse=True,
            )
            top: dict[str, Any] = {
                str(name): {
                    "tokens": int((entry or {}).get("input_tokens", 0))
                    + int((entry or {}).get("output_tokens", 0)),
                    "cost": float((entry or {}).get("cost") or 0.0),
                }
                for name, entry in ranked[:MAX_PER_MODEL]
            }
            if len(ranked) > MAX_PER_MODEL:
                rest = ranked[MAX_PER_MODEL:]
                top["others"] = {
                    "tokens": sum(
                        int((entry or {}).get("input_tokens", 0))
                        + int((entry or {}).get("output_tokens", 0))
                        for _, entry in rest
                    ),
                    "cost": sum(float((entry or {}).get("cost") or 0.0) for _, entry in rest),
                }
            rows.append(
                _row(
                    surface,
                    session_id,
                    event.ts,
                    "cost",
                    {
                        "turn_id": turn_id,
                        "tokens": int(payload.get("tokens", 0)),
                        "cost": float(payload.get("cost", 0.0)),
                        "per_model": top,
                    },
                )
            )
    return rows


def fit_line(record: dict[str, Any]) -> bytes:
    """构造单行字节（含换行，≤ MAX_LINE_BYTES）。

    调用方已按字段上限截断；仍超（中文按字节算更容易超）就对正文类字段逐次
    减半并置 truncated=true。全缩到空还超 = 结构本身病态（如超长 tool_name），
    抛 ValueError 由写者吞掉并记日志——绝不让单行撑爆 4KB 承诺。
    """
    candidate = copy.deepcopy(record)
    while True:
        line = (json.dumps(candidate, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(line) <= MAX_LINE_BYTES:
            return line
        data = candidate.get("data")
        shrunk = False
        if isinstance(data, dict):
            for key in _SHRINK_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value:
                    data[key] = value[: len(value) // 2]
                    data["truncated"] = True
                    shrunk = True
                    break
        if not shrunk:
            raise ValueError(f"L1 行无法压进 {MAX_LINE_BYTES} 字节：event={record.get('event')!r}")


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


class TraceWriter:
    """L1 写侧：per-surface 锁 + 锁内数行钉 seq + 失败只 log。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, surface: str) -> asyncio.Lock:
        lock = self._locks.get(surface)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[surface] = lock
        return lock

    async def append(self, surface: str, rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
        """追加一批行（一个回合批），返回每行的 (文件名, 行号)。

        行号在锁内每次重数（月文件行数有界，换「重启后、多写者也不漂」）；同一
        回合的归属月取首行 ts。写失败 log 吞掉，返回已写部分。
        """
        if not rows:
            return []
        if not _SURFACE_RE.fullmatch(surface):
            raise ValueError(f"非法 surface {surface!r}")
        written: list[tuple[str, int]] = []
        async with self._lock(surface):
            try:
                ym = time.strftime("%Y-%m", time.localtime(float(rows[0].get("ts") or time.time())))
                path = paths.trace_path(self._data_root, surface, ym)
                path.parent.mkdir(parents=True, exist_ok=True)
                count = count_lines(path)
                with path.open("ab") as fh:
                    for row in rows:
                        record = copy.deepcopy(row)
                        data = record.setdefault("data", {})
                        data["seq"] = count + 1
                        try:
                            line = fit_line(record)
                        except ValueError as exc:
                            logger.warning("L1 行丢弃：%s", exc)
                            continue
                        fh.write(line)
                        count += 1
                        written.append((path.name, count))
            except OSError:
                logger.exception("L1 轨迹写失败（surface=%s）", surface)
        return written


def list_files(data_root: Path, surface: str) -> list[str]:
    """该 surface 的月文件（文件名升序）。"""
    if not _SURFACE_RE.fullmatch(surface):
        return []
    directory = paths.trace_dir(data_root, surface)
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("*.jsonl") if _FILE_RE.fullmatch(path.name))


def read_page(
    data_root: Path,
    surface: str,
    filename: str,
    *,
    offset: int = 0,
    limit: int = 200,
) -> tuple[int, list[dict[str, Any]]]:
    """读一页 L1 行（给工作台浏览）；坏行跳过；返回 (总行数, 页内行)。"""
    if not _SURFACE_RE.fullmatch(surface) or not _FILE_RE.fullmatch(filename):
        return 0, []
    path = paths.trace_dir(data_root, surface) / filename
    if not path.exists():
        return 0, []
    total = 0
    page: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for index, raw in enumerate(fh, start=1):
            total = index
            if index <= offset or len(page) >= limit:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                page.append(row)
    return total, page


def file_stats(path: Path) -> dict[str, Any]:
    """文件行数/字节数与各事件计数（概览用）；坏行只计数不分类。"""
    stats: dict[str, Any] = {
        "lines": 0,
        "bytes": path.stat().st_size if path.exists() else 0,
        "events": {},
    }
    if not path.exists():
        return stats
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            stats["lines"] += 1
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                kind = str(row.get("event", "?"))
                stats["events"][kind] = stats["events"].get(kind, 0) + 1
    return stats


def read_since(
    data_root: Path, surface: str, cursor: dict[str, Any] | None
) -> list[tuple[str, int, dict[str, Any]]]:
    """读水位之后的行（consolidator update 的输入），按 (文件, 行号) 升序。

    返回 [(文件名, 行号, 行)]。翻月/补空档规则：文件名 > 水位的从头读，
    == 水位的从 line+1 读，< 水位的跳过。
    """
    if not _SURFACE_RE.fullmatch(surface):
        return []
    directory = paths.trace_dir(data_root, surface)
    if not directory.is_dir():
        return []
    cursor_file = str((cursor or {}).get("file", ""))
    cursor_line = int((cursor or {}).get("line", 0))
    out: list[tuple[str, int, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.jsonl")):
        name = path.name
        if not _FILE_RE.fullmatch(name) or name < cursor_file:
            continue
        start = cursor_line + 1 if name == cursor_file else 1
        with path.open("r", encoding="utf-8") as fh:
            for index, raw in enumerate(fh, start=1):
                if index < start:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append((name, index, row))
    return out


def parse_ref(ref: str) -> tuple[str, str, int] | None:
    """解析引用体（无方括号）`chat/2026-09.jsonl#123` → (surface, file, line)。"""
    match = _REF_RE.match(ref)
    if match is None:
        return None
    return match.group("surface"), match.group("file"), int(match.group("line"))


def resolve_ref(data_root: Path, ref: str) -> dict[str, Any] | None:
    """回读引用目标行；文件/行缺失或 data.seq 与行号不符 → None（引用失效）。"""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    surface, filename, line_no = parsed
    path = paths.trace_dir(data_root, surface) / filename
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        for index, raw in enumerate(fh, start=1):
            if index > line_no:
                break
            if index != line_no:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if isinstance(row, dict) and (row.get("data") or {}).get("seq") == line_no:
                return row
            return None
    return None
