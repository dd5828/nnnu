"""引用规范化与校验（§7.10）：模型输出只准用引用池里的行号；游标外一律拒绝。

- 模型给的引用可能带不带 `L1:`/`L2:` 前缀、L2 并包里是逗号分开的——统一在此
  归一到存储口径（带前缀、无方括号），归一失败的直接判无效（宁缺毋滥）。
- 校验分两级：池校验（模型说的事实必须落在本批喂给它的行号里）与回读校验
  （L1 回读原始行 + seq 对账；L2 查目标条目还在不在——删掉的有 tombstone）。
"""

import re
from pathlib import Path

from nnnu.services.memory import trace
from nnnu.services.memory.store import MemoryStore

_L1_BODY_RE = re.compile(r"^[a-z0-9_]+/\d{4}-\d{2}\.jsonl#[1-9]\d*$")
_L2_BODY_RE = re.compile(r"^[a-z0-9_]+#mem-[0-9a-f]{8}$")
_L2_REF_RE = re.compile(r"^L2:(?P<surface>[a-z0-9_]+)#(?P<id>mem-[0-9a-f]{8})$")


def l1_ref_body(surface: str, filename: str, line: int) -> str:
    """引用体（无前缀无方括号）：`chat/2026-09.jsonl#123`。"""
    return f"{surface}/{filename}#{line}"


def normalize_l1_ref(raw: str) -> str | None:
    """`chat/2026-09.jsonl#123` / `L1:chat/…` → `L1:chat/…`；不合形返回 None。"""
    body = raw.strip()
    if body.startswith("L1:"):
        body = body[3:]
    return f"L1:{body}" if _L1_BODY_RE.fullmatch(body) else None


def normalize_l2_ref(raw: str) -> str | None:
    """`chat#mem-…` / `L2:chat#mem-…` → `L2:chat#mem-…`；不合形返回 None。"""
    body = raw.strip()
    if body.startswith("L2:"):
        body = body[3:]
    return f"L2:{body}" if _L2_BODY_RE.fullmatch(body) else None


def parse_l2_ref(ref: str) -> tuple[str, str] | None:
    match = _L2_REF_RE.fullmatch(ref)
    if match is None:
        return None
    return match.group("surface"), match.group("id")


def l2_ref_of(surface: str, entry_id: str) -> str:
    """引用体（无前缀，与 l1_ref_body 同口径）：`chat#mem-…`；落库时才加 L2:。"""
    return f"{surface}#{entry_id}"


def pool_bodies(units: list[tuple[str, str]]) -> set[str]:
    """本批喂给模型的引用池（引用体集合：L1 为 `surface/file#line`，L2 为 `surface#mem-…`）。"""
    return {ref for ref, _ in units}


def broken_l1_refs(data_root: Path, refs: list[str]) -> list[str]:
    """回读校验：文件/行缺失或 seq 对不上 → 失效。只查 L1 前缀的。"""
    broken: list[str] = []
    for ref in refs:
        if not ref.startswith("L1:"):
            continue
        if trace.resolve_ref(data_root, ref[3:]) is None:
            broken.append(ref)
    return broken


def broken_l2_refs(store: MemoryStore, tombstone_ids: set[str], refs: list[str]) -> list[str]:
    """L2 目标校验：条目不存在（含被删 tombstone）→ 失效。"""
    broken: list[str] = []
    for ref in refs:
        if not ref.startswith("L2:"):
            continue
        parsed = parse_l2_ref(ref)
        if parsed is None or parsed[1] in tombstone_ids:
            broken.append(ref)
            continue
        surface, entry_id = parsed
        if store.find_in("l2", surface, entry_id) is None:
            broken.append(ref)
    return broken
