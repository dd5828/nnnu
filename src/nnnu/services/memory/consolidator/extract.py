"""语义抽取（§7.10 补做）：L2/L3 条目 → 实体/关系，落 semantic.json。

顺序即短路顺序（前几步都不烧 LLM）：
预算 0 → 无条目 → 源哈希未变（工件已是最新）→ 才发请求。
每块一次调用；`ask_json` 返回 None（预算尽/解析失败）→ 本轮不写盘、下轮重试——
与 update 同一模式：宁可整份重来，不落半份工件。

抽取只喂「最近 budget_extract × 每块字数」的窗口（老条目关系随窗口滚出会衰减，
先按此上线；`source` 仍记全量哈希，任何改动都会触发重抽）。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from nnnu.services.memory import paths, semantic
from nnnu.services.memory.consolidator.chunk import chunk_units
from nnnu.services.memory.models import MemoryEntry
from nnnu.services.settings.atomic import atomic_write_json

if TYPE_CHECKING:
    from nnnu.services.memory.consolidator.pipeline import Consolidator

EXTRACT_CHUNK_CHARS = 4000
EXTRACT_MAX_TOKENS = 2400


def select_recent(entries: list[MemoryEntry], max_chars: int) -> list[MemoryEntry]:
    """最近优先窗口：按 (日期, id) 升序后从尾部往回攒，超字数即停；保持原顺序返回。"""
    ordered = sorted(entries, key=lambda entry: (entry.date, entry.id or ""))
    picked: list[MemoryEntry] = []
    size = 0
    for entry in reversed(ordered):
        cost = len(entry.text) + len(entry.id or "") + 4
        if picked and size + cost > max_chars:
            break
        picked.append(entry)
        size += cost
    picked.reverse()
    return picked


async def extract_semantic(host: Consolidator) -> None:
    if not host.can_use("extract"):
        host.note("extract: 预算 0，跳过")
        return
    entries = semantic.all_entries(host.store)
    if not entries:
        return  # 空记忆：零 LLM（空态整合「零调用」的契约靠这条短路）
    source = semantic.source_map(entries)
    existing = semantic.load_semantic(host.data_root)
    if existing is not None and existing.get("source") == source:
        host.note("extract: 源未变化，跳过")
        return

    entry_text = {entry.id: entry.text for entry in entries if entry.id}
    window = select_recent(entries, host.config.budget_extract * EXTRACT_CHUNK_CHARS)
    units = [(entry.id or "", f"[{entry.date}] {entry.text}") for entry in window]
    nodes_list: list[list[dict[str, Any]]] = []
    edges_list: list[list[dict[str, Any]]] = []
    dropped = {"dropped_entities": 0, "dropped_relations": 0}
    calls = 0
    for chunk in chunk_units(units, EXTRACT_CHUNK_CHARS):
        allowed = {ref for ref, _ in chunk}
        events_text = "\n".join(f"[{ref}] {text}" for ref, text in chunk)
        data = await host.ask_json(
            "extract",
            host.render("extract.system"),
            host.render("extract.user", entries=events_text),
            max_tokens=EXTRACT_MAX_TOKENS,
        )
        if data is None:
            host.note("extract: 本批未吃下，本轮不落盘，下轮重试")
            return
        calls += 1
        nodes, edges, stats = semantic.validate_chunk(data, allowed_ids=allowed)
        nodes_list.append(nodes)
        edges_list.append(edges)
        for key, value in stats.items():
            dropped[key] = dropped.get(key, 0) + value

    nodes, edges = semantic.merge_partials(nodes_list, edges_list, entry_text)
    atomic_write_json(
        paths.semantic_path(host.data_root),
        {
            "version": semantic.SEMANTIC_VERSION,
            "updated_at": time.time(),
            "lang": host.lang,
            "source": source,
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "entities": len(nodes),
                "relations": len(edges),
                "dropped_entities": dropped["dropped_entities"],
                "dropped_relations": dropped["dropped_relations"],
                "llm_calls": calls,
            },
        },
    )
    host.count("semantic_entities", len(nodes))
    host.count("semantic_relations", len(edges))
    host.note(f"extract: entities={len(nodes)} relations={len(edges)}")
