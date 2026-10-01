"""语义知识图谱工件（§7.10 补做）：实体/关系抽取的校验、合并与读取。

- 工件是派生数据：`data/user/memory/semantic.json`，可整份重写；不进 state.json，
  不参与人工编辑保护（人工改的是 L2/L3，源头改了下轮重抽）；
- **fail-closed**：实体/关系一律要证据——refs 必须落在本批输入池内且形如
  mem-xxxxxxxx，没有有效 refs 的直接丢（与 update 的「引用链不可断」同一哲学）；
- `source` 记全部 L2+L3 条目的正文哈希：任一变化（新增/编辑/删除）触发重抽；
  API 侧据此实时算 stale，不落盘。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nnnu.services.memory import paths
from nnnu.services.memory.models import MemoryEntry, normalize_text, text_digest
from nnnu.services.memory.store import MemoryStore

SEMANTIC_VERSION = 1
ENTITY_TYPES: tuple[str, ...] = ("person", "topic", "project", "preference", "other")
ENTITY_NAME_MIN = 2
ENTITY_NAME_MAX = 24
ENTITY_MAX = 40
RELATION_MAX = 60
REFS_MAX = 5
RELATION_TYPE_MAX = 12
SAMPLES_PER_ENTITY = 2
SAMPLE_CHARS = 80
# 代词/泛化词/系统词：当实体没有信息量，一律不算
BLOCKED_NAMES = {
    "用户",
    "我",
    "你",
    "他",
    "她",
    "它",
    "我们",
    "他们",
    "助手",
    "系统",
    "user",
    "users",
    "assistant",
    "nnnu",
    "you",
    "me",
    "they",
}
MEM_ID_RE = re.compile(r"^mem-[0-9a-f]{8}$")


def all_entries(store: MemoryStore) -> list[MemoryEntry]:
    """抽取输入 = 可引用全集：L2 七面 + L3 四篇里带 id 的条目。"""
    entries: list[MemoryEntry] = []
    for surface in paths.SURFACES:
        entries.extend(entry for entry in store.load("l2", surface).entries if entry.id)
    for doc in paths.L3_DOCS:
        entries.extend(entry for entry in store.load("l3", doc).entries if entry.id)
    return entries


def source_map(entries: list[MemoryEntry]) -> dict[str, str]:
    """id → 正文哈希（工件的新鲜度凭据）。"""
    return {entry.id: text_digest(entry.text) for entry in entries if entry.id}


def load_semantic(data_root: Path) -> dict[str, Any] | None:
    """读工件；缺失/坏 JSON/版本不符 → None（当作没有，重抽覆盖）。"""
    path = paths.semantic_path(data_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SEMANTIC_VERSION:
        return None
    return payload


def _clean_refs(raw: Any, allowed_ids: set[str]) -> list[str]:
    """refs → 合法 id 列表（去重保序、封顶）；接受 `mem-…` / `surface#mem-…` 写法。"""
    kept: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        body = item.strip()
        if body.startswith("L2:"):
            body = body[3:]
        if "#" in body:
            body = body.rsplit("#", 1)[-1]
        if not MEM_ID_RE.fullmatch(body) or body not in allowed_ids:
            continue
        if body not in kept:
            kept.append(body)
    return kept[:REFS_MAX]


def _clean_name(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    name = normalize_text(raw)
    if not ENTITY_NAME_MIN <= len(name) <= ENTITY_NAME_MAX:
        return None
    if name.casefold() in BLOCKED_NAMES:
        return None
    return name


def validate_chunk(
    data: Any, *, allowed_ids: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """一块模型输出 → 干净的实体/关系 + 丢弃计数（块内按名归并）。"""
    stats = {"dropped_entities": 0, "dropped_relations": 0}
    raw_entities = data.get("entities") if isinstance(data, dict) else None
    raw_relations = data.get("relations") if isinstance(data, dict) else None

    entities: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for item in raw_entities if isinstance(raw_entities, list) else []:
        if not isinstance(item, dict):
            stats["dropped_entities"] += 1
            continue
        name = _clean_name(item.get("name"))
        refs = _clean_refs(item.get("refs"), allowed_ids)
        if name is None or not refs:
            stats["dropped_entities"] += 1
            continue
        type_ = item.get("type") if item.get("type") in ENTITY_TYPES else "other"
        key = name.casefold()
        current = by_key.get(key)
        if current is None:
            current = {"id": name, "name": name, "type": type_, "refs": list(refs)}
            by_key[key] = current
            entities.append(current)
        else:
            for ref in refs:
                if ref not in current["refs"] and len(current["refs"]) < REFS_MAX:
                    current["refs"].append(ref)
            if current["type"] == "other" and type_ != "other":
                current["type"] = type_

    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_relations if isinstance(raw_relations, list) else []:
        if not isinstance(item, dict):
            stats["dropped_relations"] += 1
            continue
        source = _clean_name(item.get("source"))
        target = _clean_name(item.get("target"))
        raw_type = item.get("type")
        type_ = normalize_text(raw_type)[:RELATION_TYPE_MAX] if isinstance(raw_type, str) else ""
        refs = _clean_refs(item.get("refs"), allowed_ids)
        if (
            source is None
            or target is None
            or not type_
            or not refs
            or source.casefold() not in by_key
            or target.casefold() not in by_key
            or source.casefold() == target.casefold()
        ):
            stats["dropped_relations"] += 1
            continue
        edge_key = (source.casefold(), target.casefold(), type_)
        if edge_key in seen:  # 同块重复关系按第一条
            continue
        seen.add(edge_key)
        relations.append({"source": source, "target": target, "type": type_, "refs": refs})
    return entities, relations, stats


def merge_partials(
    nodes_list: list[list[dict[str, Any]]],
    edges_list: list[list[dict[str, Any]]],
    entry_text: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """跨块合并 + 封顶 + 计数/样例；只保留两端存活的实体后接关系。"""
    merged: dict[str, dict[str, Any]] = {}
    for nodes in nodes_list:
        for node in nodes:
            key = node["name"].casefold()
            current = merged.get(key)
            if current is None:
                merged[key] = {**node, "refs": list(node["refs"])}
                continue
            for ref in node["refs"]:
                if ref not in current["refs"] and len(current["refs"]) < REFS_MAX:
                    current["refs"].append(ref)
            if current["type"] == "other" and node["type"] != "other":
                current["type"] = node["type"]

    relations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edges in edges_list:
        for edge in edges:
            key = (edge["source"].casefold(), edge["target"].casefold(), edge["type"])
            current = relations.get(key)
            if current is None:
                relations[key] = {**edge, "refs": list(edge["refs"])}
                continue
            for ref in edge["refs"]:  # 同名关系跨块出现：refs 并集，证据不丢
                if ref not in current["refs"] and len(current["refs"]) < REFS_MAX:
                    current["refs"].append(ref)

    nodes = sorted(merged.values(), key=lambda node: (-len(node["refs"]), node["name"]))[
        :ENTITY_MAX
    ]
    alive = {node["name"].casefold() for node in nodes}
    edges = [
        edge
        for edge in relations.values()
        if edge["source"].casefold() in alive and edge["target"].casefold() in alive
    ][:RELATION_MAX]
    for node in nodes:
        node["count"] = len(node["refs"])
        node["samples"] = [
            entry_text.get(ref, "")[:SAMPLE_CHARS] for ref in node["refs"][:SAMPLES_PER_ENTITY]
        ]
    for edge in edges:
        edge["count"] = len(edge["refs"])
    return nodes, edges


def graph_view(data_root: Path, store: MemoryStore) -> dict[str, Any]:
    """图谱 API 负载：无工件给空态；stale 实时比对（改过源头但还没重抽）。"""
    payload = load_semantic(data_root)
    if payload is None:
        return {
            "mode": "semantic",
            "updated_at": 0,
            "stale": False,
            "nodes": [],
            "edges": [],
            "stats": {},
        }
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    return {
        "mode": "semantic",
        "updated_at": payload.get("updated_at", 0),
        "stale": payload.get("source") != source_map(all_entries(store)),
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
    }
