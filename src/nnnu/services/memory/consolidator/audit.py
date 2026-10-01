"""audit 模式（§7.10）：引用完整性校对 + 可选 LLM 修订。

① 确定性检查（永远跑、零 LLM）：逐条回读引用——
   - `L1:` 目标行必须存在且 `data.seq` 与行号对账（带外编辑会让引用失效）；
   - `L2:` 目标条目必须还在（删掉的有 tombstone 记账）。
   失效 → 条目前置 `[STALE]`（**不删文本**）；恢复有效 → 摘掉标记。
   匿名条目（无 mem-id，手写产物）在这里补 id 并登记为人工所有——从此受保护。

② LLM 修订（预算内）：把「行号视图 + 来源证据」交给模型，改的是正文措辞，
   应用走**倒序**（行号位移安全）；保护条目（人工编辑/带外改动）不在可改集，
   op 打到保护行直接丢弃并计数。
"""

from typing import TYPE_CHECKING, Any

from nnnu.services.memory import trace
from nnnu.services.memory.consolidator import refs
from nnnu.services.memory.consolidator.guards import find_banned, truncate
from nnnu.services.memory.models import MemoryDoc, MemoryEntry, normalize_text, text_digest
from nnnu.services.memory.state import doc_key

if TYPE_CHECKING:
    from nnnu.services.memory.consolidator.pipeline import Consolidator

_TEXT_LIMITS = {"l2": 400, "l3": 300}
_EVIDENCE_CAP = 1800
_EVIDENCE_ITEM = 160


def _stale_flag_needed(host: "Consolidator", entry: MemoryEntry) -> bool:
    """该条目是否「有失效引用」（决定 STALE 标记该不该在）。"""
    if not entry.refs:
        return False  # 无引用可查：不动
    tombstones = host.state.tombstone_ids()
    broken = refs.broken_l1_refs(host.data_root, entry.refs) + refs.broken_l2_refs(
        host.store, tombstones, entry.refs
    )
    return bool(broken)


def _assign_missing_ids(host: "Consolidator", layer: str, key: str, doc: MemoryDoc) -> int:
    """匿名条目补 id：登记为人工所有（edited=True），从此进保护集。"""
    from nnnu.core.ids import new_id

    assigned = 0
    for entry in doc.entries:
        if entry.id is None:
            entry.id = new_id("mem")
            host.state.mark_edited(doc_key(layer, key), entry.id, text_digest(entry.text))
            host.count("ids_assigned")
            assigned += 1
    return assigned


async def deterministic_audit(host: "Consolidator", layer: str, key: str) -> None:
    """零 LLM 的引用对账：STALE 标记的加/摘 + 匿名补 id。"""
    doc = host.store.load(layer, key)
    if not doc.entries:
        return
    needs_fix = any(
        entry.id is None or _stale_flag_needed(host, entry) != entry.stale for entry in doc.entries
    )
    if not needs_fix:
        return

    changes = {"stale": 0, "clear": 0}

    def mutate(fresh: MemoryDoc) -> None:
        _assign_missing_ids(host, layer, key, fresh)
        for entry in fresh.entries:
            should = _stale_flag_needed(host, entry)
            if should == entry.stale:
                continue
            entry.stale = should
            changes["stale" if should else "clear"] += 1

    host.touch(layer, key)
    await host.store.apply(layer, key, mutate)
    host.count("stale_marked", changes["stale"])
    host.count("stale_cleared", changes["clear"])
    if changes["stale"] or changes["clear"]:
        host.note(f"audit:{layer}/{key} stale+{changes['stale']} -{changes['clear']}")


def _evidence_for(host: "Consolidator", entry: MemoryEntry, *, limit: int) -> str:
    """把条目的引用解析成短证据（喂给修订提示词，不喂全文）。"""
    parts: list[str] = []
    size = 0
    for ref in entry.refs:
        snippet = ""
        if ref.startswith("L1:"):
            row = trace.resolve_ref(host.data_root, ref[3:])
            if row is not None:
                data = row.get("data") or {}
                snippet = str(data.get("text") or data.get("summary") or data.get("question") or "")
        else:
            parsed = refs.parse_l2_ref(ref)
            if parsed is not None:
                target = host.store.find_in("l2", parsed[0], parsed[1])
                if target is not None:
                    snippet = target.text
        if not snippet:
            snippet = "（来源失效）"
        piece = f"- {ref}: {snippet[:_EVIDENCE_ITEM]}"
        if size + len(piece) > limit:
            break
        parts.append(piece)
        size += len(piece)
    return "\n".join(parts) or "（无）"


async def revise_doc(host: "Consolidator", layer: str, key: str) -> None:
    """LLM 修订（预算内）：只改正文，倒序应用。"""
    doc = host.store.load(layer, key)
    editable = [
        entry
        for entry in doc.entries
        if entry.id is not None and not host.is_protected(layer, key, entry)
    ]
    if not editable:
        return
    entries_text = "\n".join(
        f"{entry.line_no}. [{entry.date}] {'[STALE] ' if entry.stale else ''}{entry.text}"
        for entry in editable
    )
    evidence = "\n\n".join(
        f"{entry.line_no}:\n{_evidence_for(host, entry, limit=400)}" for entry in editable
    )[:_EVIDENCE_CAP]
    data: Any = await host.ask_json(
        "audit",
        host.render("audit.system"),
        host.render("audit.user", doc=f"{layer}/{key}", entries=entries_text, evidence=evidence),
    )
    if data is None:
        return
    edits = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits, list) or not edits:
        return

    by_line = {entry.line_no: entry for entry in editable}
    ops: list[tuple[int, MemoryEntry, str]] = []
    for item in edits:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        raw_text = item.get("text")
        if not isinstance(line, int) or not isinstance(raw_text, str):
            continue
        entry = by_line.get(line)
        if entry is None:  # 命中保护行/不存在行：丢弃（保护条目永不被改）
            host.count("skipped_protected")
            continue
        text = truncate(raw_text, _TEXT_LIMITS.get(layer, 400))
        if not text:
            continue
        if find_banned(text):
            host.count("banned_dropped")
            continue
        if normalize_text(text) == normalize_text(entry.text):
            continue
        ops.append((line, entry, text))
    if not ops:
        return

    def mutate(fresh: MemoryDoc) -> None:
        fresh_by_line = {entry.line_no: entry for entry in fresh.entries}
        for line, _, text in sorted(ops, key=lambda op: op[0], reverse=True):
            target = fresh_by_line.get(line)
            if target is not None:
                target.text = text

    host.touch(layer, key)
    await host.store.apply(layer, key, mutate)
    state_key = doc_key(layer, key)
    for _, entry, text in ops:
        host.state.refresh_entry(
            state_key, entry.id or "", text_digest(text), origin="consolidator"
        )
        host.count("audit_edits")
    host.note(f"audit:{layer}/{key} ~{len(ops)}")
