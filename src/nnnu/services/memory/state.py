"""state.json（§7.10 / §8.1）：consolidator 水位 + 人工编辑哈希保护 + last_run。

- 水位 `trace.<surface> = {file, line}`：该 surface 已消费到哪一行；update 只吃
  水位之后的新事件（翻月/补空档规则在 trace.read_since）。
- `entries = {"L2/chat.md": {"mem-…": {hash, edited, origin, created_at}}}`：
  hash 是条目**正文**（去日期与引用、空白归一）的 sha256——改引用/日期不算人工
  编辑；`edited=true` 是保护开关（consolidator 的「跳过」依据），`origin ∈
  consolidator|model|human` 供前端打标。
- `tombstones`：删掉的条目 id 坟场（上限 200 FIFO）——L3 引用的 L2 id 被删后，
  下轮 audit 能据此把引用判失效标 STALE。
- 读写全走 atomic_write_json（与 settings/secrets 同款公共工具）；文件损坏
  → log + 回默认值，绝不因账本坏了挡住记忆功能。
"""

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any

from nnnu.services.memory import paths
from nnnu.services.settings.atomic import atomic_write_json

logger = logging.getLogger(__name__)

TOMBSTONE_CAP = 200


def doc_key(layer: str, key: str) -> str:
    """state.entries 的文档键：L2/chat.md / L3/profile.md。"""
    return f"{'L2' if layer == 'l2' else 'L3'}/{key}.md"


def _defaults() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": 0.0,
        "trace": {},
        "turns_since_consolidation": 0,
        "entries": {},
        "l3_seen": {},
        "tombstones": [],
        "last_run": None,
    }


class StateStore:
    """state.json 门面：读一次缓存、每次改动即原子落盘。"""

    def __init__(self, data_root: Path) -> None:
        self._path = paths.state_path(data_root)
        self._state: dict[str, Any] | None = None

    # ---- 基础 ----

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        state = _defaults()
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.exception("state.json 读取失败，回默认值")
                raw = None
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key not in state:
                        continue
                    default = state[key]
                    if default is None or isinstance(value, type(default)):
                        state[key] = value
                    elif isinstance(default, float) and isinstance(value, int):
                        state[key] = float(value)  # JSON 里 1759… 会读成 int
        self._state = state
        return state

    def _save(self) -> None:
        state = self._load()
        state["updated_at"] = time.time()
        atomic_write_json(self._path, state)

    def reload(self) -> None:
        """丢缓存重读（测试/带外改动后手动同步用）。"""
        self._state = None

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    # ---- 回合计数与水位 ----

    def turns_since(self) -> int:
        return int(self._load()["turns_since_consolidation"])

    def bump_turn(self) -> int:
        state = self._load()
        state["turns_since_consolidation"] = int(state["turns_since_consolidation"]) + 1
        self._save()
        return int(state["turns_since_consolidation"])

    def reset_turns(self) -> None:
        self._load()["turns_since_consolidation"] = 0
        self._save()

    def watermark(self, surface: str) -> dict[str, Any]:
        cursor = self._load()["trace"].get(surface)
        if not isinstance(cursor, dict):
            return {"file": "", "line": 0}
        return {"file": str(cursor.get("file", "")), "line": int(cursor.get("line", 0))}

    def set_watermark(self, surface: str, file: str, line: int) -> None:
        self._load()["trace"][surface] = {"file": file, "line": line}
        self._save()

    # ---- 条目哈希账本 ----

    def doc_entries(self, key: str) -> dict[str, dict[str, Any]]:
        """该文档的条目账（缺则就地建空账挂回 state——返回的必须是活引用）。"""
        entries = self._load()["entries"]
        doc = entries.get(key)
        if not isinstance(doc, dict):
            doc = {}
            entries[key] = doc
        return doc

    def entry_meta(self, key: str, entry_id: str) -> dict[str, Any] | None:
        meta = self.doc_entries(key).get(entry_id)
        return meta if isinstance(meta, dict) else None

    def register_entry(self, key: str, entry_id: str, digest: str, origin: str) -> None:
        """新条目登记（edited=False）。"""
        self.doc_entries(key)[entry_id] = {
            "hash": digest,
            "edited": False,
            "origin": origin,
            "created_at": time.time(),
        }
        self._save()

    def refresh_entry(
        self, key: str, entry_id: str, digest: str, origin: str | None = None
    ) -> None:
        """consolidator 自己改写后刷新哈希；edited 保护位保留不动。"""
        meta = self.doc_entries(key).get(entry_id)
        if not isinstance(meta, dict):
            self.register_entry(key, entry_id, digest, origin or "consolidator")
            return
        meta["hash"] = digest
        if origin is not None:
            meta["origin"] = origin
        self._save()

    def mark_edited(self, key: str, entry_id: str, digest: str | None = None) -> None:
        """人工编辑：edited=True（保护开关），origin=human。"""
        meta = self.doc_entries(key).get(entry_id)
        if not isinstance(meta, dict):
            meta = {"created_at": time.time(), "origin": "human"}
            self.doc_entries(key)[entry_id] = meta
        if digest is not None:
            meta["hash"] = digest
        meta["edited"] = True
        meta["origin"] = "human"
        self._save()

    def set_managed(self, key: str, entry_id: str, digest: str) -> None:
        """交还自动管理：edited=False，origin=consolidator，哈希对齐当前文本。"""
        meta = self.doc_entries(key).get(entry_id)
        if not isinstance(meta, dict):
            meta = {"created_at": time.time()}
            self.doc_entries(key)[entry_id] = meta
        meta["hash"] = digest
        meta["edited"] = False
        meta["origin"] = "consolidator"
        self._save()

    def drop_entry_meta(self, key: str, entry_id: str) -> None:
        self.doc_entries(key).pop(entry_id, None)
        self._save()

    # ---- tombstone ----

    def add_tombstone(self, entry_id: str, layer: str, file: str) -> None:
        tombstones = self._load()["tombstones"]
        tombstones.append({"id": entry_id, "layer": layer, "file": file, "deleted_at": time.time()})
        del tombstones[:-TOMBSTONE_CAP]  # 超出上限丢最旧（FIFO）
        self._save()

    def tombstone_ids(self) -> set[str]:
        return {str(t.get("id")) for t in self._load()["tombstones"] if isinstance(t, dict)}

    # ---- L3 输入去重（surface 维度已看过哪些 L2 条目）----

    def l3_seen(self, surface: str) -> dict[str, str]:
        seen = self._load()["l3_seen"].get(surface)
        return seen if isinstance(seen, dict) else {}

    def mark_l3_seen(self, surface: str, mapping: dict[str, str]) -> None:
        self._load()["l3_seen"][surface] = dict(mapping)
        self._save()

    # ---- 运行账 ----

    def set_last_run(self, run: dict[str, Any]) -> None:
        self._load()["last_run"] = dict(run)
        self._save()

    def last_run(self) -> dict[str, Any] | None:
        run = self._load()["last_run"]
        return dict(run) if isinstance(run, dict) else None

    def mark_interrupted(self) -> bool:
        """启动恢复：上次运行卡在 running → 记 interrupted；有则返回 True。"""
        run = self._load()["last_run"]
        if isinstance(run, dict) and run.get("status") == "running":
            run["status"] = "interrupted"
            run["finished_at"] = time.time()
            self._save()
            return True
        return False
