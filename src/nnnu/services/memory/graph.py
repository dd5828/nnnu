"""记忆证据链组装（§7.10）：L3 条目 → L2 源条目 → L1 原始行，逐层展开给图谱。

- `build_graph(...)` 两种入口（`GET /memory/graph` 唯一实现）：
  - `entry` 为空 → 全景：L3 四篇 + L2 七面的全部条目，L3 的 `[L2:…]` 引用连边；
  - `entry` 指定（L2/L3 条目 id）→ 以它为根按 `depth` 展开。**depth=2 就是验收②
    的「点 L3 节点看到 L1 证据链」**：L3 → 引用的 L2 条目 → 各自的 L1 原始行。
- 引用断了（目标条目/行已删，或 L1 行 seq 校验不过）不给「凭空消失」，而是给
  `broken=true` 的占位节点——前端能看见断链，正好和 audit 的 `[STALE]` 对得上。
- 只读：不碰锁、不改 state（`entry_view` 读账本拼展示值）。
"""

import time
from typing import Any

from nnnu.services.memory import paths, trace
from nnnu.services.memory.models import MemoryEntry
from nnnu.services.memory.state import StateStore, doc_key
from nnnu.services.memory.store import MemoryStore


def entry_view(state: StateStore, entry: MemoryEntry) -> dict[str, Any]:
    """条目 → 展示视图：origin/edited 以 state 账本为准（模型里只是默认值）。"""
    view = entry.to_dict()
    meta = state.entry_meta(doc_key(entry.layer, entry.key), entry.id) if entry.id else None
    if meta is not None:
        view["edited"] = bool(meta.get("edited"))
        view["origin"] = str(meta.get("origin", entry.origin))
    elif entry.id is None:
        # 手写未纳管：按人工产物展示（下轮 audit 补 id 并标人工所有）
        view["origin"] = "human"
        view["edited"] = True
    return view


def _l1_text(row: dict[str, Any]) -> str:
    """L1 行 → 一句可读的展示文本（图谱节点/抽屉标题用）。"""
    data = row.get("data") or {}
    kind = str(row.get("event", "?"))
    if kind in ("user_message", "assistant_done"):
        return str(data.get("text", ""))
    if kind == "tool_call":
        name = str(data.get("tool_name", ""))
        detail = str(data.get("summary") or data.get("args_preview") or "")
        return f"{name}: {detail}" if detail else name
    if kind == "ask_user":
        return str(data.get("question", ""))
    if kind == "cost":
        return f"{data.get('tokens', 0)} tokens"
    return kind


def _l1_node(ref_body: str, row: dict[str, Any] | None) -> dict[str, Any]:
    surface = ref_body.split("/", 1)[0] if "/" in ref_body else ref_body
    if row is None:  # 文件没了 / 行没了 / seq 对不上
        return {
            "id": ref_body,
            "layer": "l1",
            "key": surface,
            "kind": "broken",
            "text": ref_body,
            "date": "",
            "ref": ref_body,
            "stale": False,
            "edited": False,
            "origin": "",
            "broken": True,
        }
    ts = row.get("ts")
    date = time.strftime("%Y-%m-%d", time.localtime(ts)) if isinstance(ts, (int, float)) else ""
    return {
        "id": ref_body,
        "layer": "l1",
        "key": surface,
        "kind": str(row.get("event", "?")),
        "text": _l1_text(row),
        "date": date,
        "ref": ref_body,
        "stale": False,
        "edited": False,
        "origin": "",
        "broken": False,
    }


class _Builder:
    """节点/边累积器：同 id 只留一份，边去重。"""

    def __init__(self, data_root, store: MemoryStore, state: StateStore) -> None:
        self._data_root = data_root
        self._store = store
        self._state = state
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, str]] = []
        self._expanded: set[str] = set()

    def add_entry(self, entry: MemoryEntry) -> str:
        node_id = entry.id or f"{entry.layer}/{entry.key}#{entry.line_no}"
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "kind": "entry",
                "broken": False,
                **entry_view(self._state, entry),
            }
        return node_id

    def link(self, source: str, target: str) -> None:
        edge = {"source": source, "target": target}
        if edge not in self.edges:
            self.edges.append(edge)

    def expand(self, entry: MemoryEntry, depth: int) -> None:
        """展开一条条目到 depth 层（depth=1 只到直接引用，2 再往下走一层）。"""
        node_id = self.add_entry(entry)
        if node_id in self._expanded or depth <= 0:
            return
        self._expanded.add(node_id)
        for ref in entry.refs:
            if ref.startswith("L2:"):
                surface, _, entry_id = ref[len("L2:") :].partition("#")
                target = self._store.find_in("l2", surface, entry_id)
                if target is None:
                    self.nodes.setdefault(
                        ref,
                        {
                            "id": ref,
                            "layer": "l2",
                            "key": surface,
                            "kind": "broken",
                            "text": entry_id,
                            "date": "",
                            "ref": ref,
                            "stale": False,
                            "edited": False,
                            "origin": "",
                            "broken": True,
                        },
                    )
                    self.link(node_id, ref)
                    continue
                self.link(node_id, target.id or ref)
                self.expand(target, depth - 1)
            else:  # L1:chat/2026-09.jsonl#123
                body = ref[len("L1:") :]
                self.nodes.setdefault(
                    body, _l1_node(body, trace.resolve_ref(self._data_root, body))
                )
                self.link(node_id, body)


def build_graph(
    data_root,
    store: MemoryStore,
    state: StateStore,
    *,
    entry: str | None = None,
    depth: int = 1,
) -> dict[str, Any] | None:
    """组装证据链；entry 指定的条目查无此条返回 None（路由 404）。"""
    builder = _Builder(data_root, store, state)
    if entry:
        found = store.find("l3", entry) or store.find("l2", entry)
        if found is None:
            return None
        _, root = found
        builder.expand(root, depth)
    else:  # 全景：L3 条目连到 L2，L2 条目本身只出节点（不自动拉 L1，避免图爆炸）
        for doc in paths.L3_DOCS:
            for item in store.load("l3", doc).entries:
                builder.expand(item, 1)
        for surface in paths.SURFACES:
            for item in store.load("l2", surface).entries:
                builder.add_entry(item)
    return {
        "root": entry,
        "depth": depth,
        "nodes": list(builder.nodes.values()),
        "edges": builder.edges,
    }
