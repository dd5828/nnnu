"""update 模式（§7.10）：L1 新事件 → L2 分面事实；L2 新条目 → L3 综合画像。

- **只 append，不改既有条目**：人工编辑保护在 update 阶段是天然的（验收③的
  第一道）。
- 每个 chunk 一次 LLM 调用；事实的 refs 必须落在本 chunk 引用池里（引用链不可
  断），空引用/池外引用/banned 句子/重复文本都在入库前滤掉。
- **水位每 chunk 推进**：本批没吃下（预算耗尽/解析失败）就停在这——下一轮从这
  重放，重放产生的重复由归一化去重兜住，最坏不丢不错。
"""

import time
from typing import TYPE_CHECKING, Any

from nnnu.services.memory import paths, trace
from nnnu.services.memory.consolidator import refs
from nnnu.services.memory.consolidator.chunk import chunk_units
from nnnu.services.memory.consolidator.guards import find_banned, truncate
from nnnu.services.memory.models import MemoryEntry, text_digest
from nnnu.services.memory.state import doc_key

if TYPE_CHECKING:
    from nnnu.services.memory.consolidator.pipeline import Consolidator

L2_TEXT_LIMIT = 400
L3_TEXT_LIMIT = 300
_EXISTING_CAP = 1500


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _date_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _existing_text(texts: list[str]) -> str:
    """已有条目文本（从最新往回取到预算）：给模型「勿重复」用。"""
    picked: list[str] = []
    size = 0
    for text in reversed(texts):
        line = f"- {text}"
        if picked and size + len(line) > _EXISTING_CAP:
            break
        picked.insert(0, line)
        size += len(line)
    return "\n".join(picked)


def _render_event(kind: str, data: dict[str, Any]) -> str | None:
    """一条 L1 行 → 给模型看的一行；cost 无记忆语义，渲染时跳过。"""
    date = _date_of(float(data.get("_ts") or 0) or time.time())
    if kind == "user_message":
        return f"{date} user_message: {data.get('text', '')}"
    if kind == "tool_call":
        summary = data.get("summary") or ""
        tail = f" — {summary}" if summary else ""
        return f"{date} tool_call: {data.get('tool_name', '')} ok={data.get('ok')}{tail}"
    if kind == "assistant_done":
        return f"{date} assistant_done: {data.get('text', '')}"
    if kind == "ask_user":
        answer = data.get("answer") or ""
        tail = f" => {answer}" if data.get("answered") and answer else ""
        return f"{date} ask_user: {data.get('question', '')}{tail}"
    return None


def _render_rows(
    surface: str, rows: list[tuple[str, int, dict[str, Any]]]
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """L1 行 → 事件单元 + ref→日期表；回合之间插空行做分组。"""
    units: list[tuple[str, str]] = []
    dates: dict[str, str] = {}
    prev_turn: str | None = None
    for filename, line, row in rows:
        data = dict(row.get("data") or {})
        data["_ts"] = row.get("ts")
        text = _render_event(str(row.get("event", "")), data)
        if text is None:
            continue
        turn = str(data.get("turn_id", ""))
        if prev_turn is not None and turn != prev_turn:
            text = "\n" + text
        prev_turn = turn
        body = refs.l1_ref_body(surface, filename, line)
        units.append((body, text))
        dates[body] = _date_of(float(row.get("ts") or 0) or time.time())
    return units, dates


def _facts_to_entries(
    host: "Consolidator", facts: Any, pool: set[str], dates: dict[str, str], surface: str
) -> list[MemoryEntry]:
    """模型 facts → 条目（引用池校验 + banned 守卫 + 截断）。"""
    entries: list[MemoryEntry] = []
    for fact in facts if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            continue
        raw_text = fact.get("text")
        if not isinstance(raw_text, str):
            continue
        text = truncate(raw_text, L2_TEXT_LIMIT)
        if not text:
            continue
        if find_banned(text):
            host.count("banned_dropped")
            continue
        kept: list[str] = []
        raw_refs = fact.get("refs")
        for raw_ref in raw_refs if isinstance(raw_refs, list) else []:
            if not isinstance(raw_ref, str):
                continue
            body = raw_ref.strip()
            if body.startswith("L1:"):
                body = body[3:]
            if body not in pool:  # 池外引用：模型编的，丢
                host.count("ref_rejected")
                continue
            normalized = refs.normalize_l1_ref(body)
            if normalized and normalized not in kept:
                kept.append(normalized)
        if not kept:  # 引用链不可断：没有有效引用的条目不入库
            host.count("ref_rejected")
            continue
        date = dates.get(kept[0][3:], _today())
        entries.append(
            MemoryEntry(id=None, date=date, text=text, refs=kept, layer="l2", key=surface)
        )
    return entries


async def update_surface(host: "Consolidator", surface: str) -> None:
    """一个分面的 L2 update：消费水位后的新事件。"""
    cursor = host.state.watermark(surface)
    rows = trace.read_since(host.data_root, surface, cursor)
    if not rows:
        return
    units, dates = _render_rows(surface, rows)
    if not units:
        # 全是 cost 之类不可渲染的行：水位照推，别让它们堵住队列
        filename, line, _ = rows[-1]
        host.state.set_watermark(surface, filename, line)
        return

    doc = host.store.load("l2", surface)
    existing = _existing_text([entry.text for entry in doc.entries])
    for chunk in chunk_units(units, host.config.update_chunk_chars):
        pool = refs.pool_bodies(chunk)
        events_text = "\n".join(f"[{ref}] {text}" for ref, text in chunk)
        data = await host.ask_json(
            "update",
            host.render("update.system"),
            host.render(
                "update.user",
                surface=surface,
                events=events_text,
                pool="、".join(sorted(pool)),
                existing=existing or "（暂无）",
            ),
        )
        if data is None:  # 预算耗尽/解析失败：停在本 chunk 之前，下轮重放（去重兜底）
            return
        entries = _facts_to_entries(
            host, data.get("facts") if isinstance(data, dict) else None, pool, dates, surface
        )
        if entries:
            host.touch("l2", surface)
            written = await host.store.append_entries("l2", surface, entries)
            key = doc_key("l2", surface)
            for entry in written:
                host.state.register_entry(
                    key, entry.id or "", text_digest(entry.text), "consolidator"
                )
            host.count("l2_added", len(written))
            host.note(f"update:{surface} +{len(written)}")
        host.count("events", len(chunk))
        last = trace.parse_ref(chunk[-1][0])
        if last is not None:
            _, filename, line = last
            host.state.set_watermark(surface, filename, line)


async def update_l3(host: "Consolidator", surfaces: tuple[str, ...] | None = None) -> None:
    """L3 update：跨分面综合，消费各 L2 里「L3 还没看过」的条目。"""
    surfaces = surfaces or paths.SURFACES
    units: list[tuple[str, str]] = []
    pending: dict[str, dict[str, str]] = {}
    for surface in surfaces:
        seen = host.state.l3_seen(surface)
        for entry in host.store.load("l2", surface).entries:
            if entry.id is None:
                continue
            digest = text_digest(entry.text)
            if seen.get(entry.id) == digest:
                continue
            units.append((refs.l2_ref_of(surface, entry.id), f"[{entry.date}] {entry.text}"))
            pending.setdefault(surface, {})[entry.id] = digest
    if not units:
        return

    existing = _existing_text(
        [
            entry.text
            for doc_name in paths.L3_DOCS
            for entry in host.store.load("l3", doc_name).entries
        ]
    )
    for chunk in chunk_units(units, host.config.update_chunk_chars):
        pool = refs.pool_bodies(chunk)
        events_text = "\n".join(f"[{ref}] {text}" for ref, text in chunk)
        data = await host.ask_json(
            "update",
            host.render("l3.system"),
            host.render(
                "l3.user",
                events=events_text,
                pool="、".join(sorted(pool)),
                existing=existing or "（暂无）",
            ),
        )
        if data is None:
            return
        by_doc: dict[str, list[MemoryEntry]] = {}
        for fact in (data.get("facts") if isinstance(data, dict) else None) or []:
            if not isinstance(fact, dict):
                continue
            doc_name = fact.get("doc")
            raw_text = fact.get("text")
            if doc_name not in paths.L3_DOCS or not isinstance(raw_text, str):
                continue
            text = truncate(raw_text, L3_TEXT_LIMIT)
            if not text or find_banned(text):
                if text:
                    host.count("banned_dropped")
                continue
            kept: list[str] = []
            raw_refs = fact.get("refs")
            for raw_ref in raw_refs if isinstance(raw_refs, list) else []:
                if not isinstance(raw_ref, str):
                    continue
                body = raw_ref.strip()
                if body.startswith("L2:"):
                    body = body[3:]
                if body not in pool:
                    host.count("ref_rejected")
                    continue
                normalized = refs.normalize_l2_ref(body)
                if normalized and normalized not in kept:
                    kept.append(normalized)
            if not kept:
                host.count("ref_rejected")
                continue
            by_doc.setdefault(doc_name, []).append(
                MemoryEntry(id=None, date=_today(), text=text, refs=kept, layer="l3", key=doc_name)
            )
        for doc_name, entries in by_doc.items():
            host.touch("l3", doc_name)
            written = await host.store.append_entries("l3", doc_name, entries)
            key = doc_key("l3", doc_name)
            for entry in written:
                host.state.register_entry(
                    key, entry.id or "", text_digest(entry.text), "consolidator"
                )
            host.count("l3_added", len(written))
            host.note(f"update:l3/{doc_name} +{len(written)}")
        # 本 chunk 已消费的 L2 条目记账（只有吃下的 chunk 才标记）
        seen_now: dict[str, dict[str, str]] = {}
        for ref, _ in chunk:
            ref_surface, _, entry_id = ref.partition("#")
            seen_digest = pending.get(ref_surface, {}).get(entry_id)
            if seen_digest:
                seen_now.setdefault(ref_surface, {})[entry_id] = seen_digest
        for surface, mapping in seen_now.items():
            merged = {**host.state.l3_seen(surface), **mapping}
            host.state.mark_l3_seen(surface, merged)
