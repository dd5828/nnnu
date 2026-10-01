"""dedup 模式（§7.10）：同文档内语义重复的合并/删除。

- **只允许 replace / delete，禁止 insert**：去重的爆炸半径必须小于信息增量，
  模型想「顺手加一条」的 op 一律丢弃。
- 每轮把当前条目（行号视图）喂给模型，ops 倒序应用；空 ops = 收敛，提前停。
- 保护条目（人工编辑/带外改动/匿名）不进输入；op 打到保护行丢弃并计数。
- delete 落 tombstone：L3 里引用它的条目会在下轮 audit 判失效标 STALE，
  引用链不会悬空指着一个不存在的目标。

「疑似重复」的确定性预判用字符二元组 Jaccard（不花 LLM 的粗筛；跟题库查重
同思路），过线的文档才值得进 dedup 回合。
"""

from typing import TYPE_CHECKING, Any

from nnnu.services.memory.consolidator.guards import find_banned, truncate
from nnnu.services.memory.models import MemoryDoc, MemoryEntry, normalize_text, text_digest
from nnnu.services.memory.state import doc_key

if TYPE_CHECKING:
    from nnnu.services.memory.consolidator.pipeline import Consolidator

_TEXT_LIMITS = {"l2": 400, "l3": 300}
_SUSPECT_THRESHOLD = 0.8


def _bigrams(text: str) -> set[str]:
    normalized = normalize_text(text)
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def suspect_duplicates(entries: list[MemoryEntry]) -> bool:
    """粗筛：任意两条正文的字符二元组 Jaccard ≥ 0.8 视为疑似重复。"""
    grams = [_bigrams(entry.text) for entry in entries]
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            left, right = grams[i], grams[j]
            if not left or not right:
                continue
            overlap = len(left & right) / len(left | right)
            if overlap >= _SUSPECT_THRESHOLD:
                return True
    return False


def _validate_ops(
    host: "Consolidator", data: Any, editable_by_line: dict[int, MemoryEntry], layer: str
) -> list[tuple[int, str, str]]:
    """模型输出 → 合法 ops [(op, line, text)]；保护行/非法 op/insert 一律丢弃。"""
    ops: list[tuple[int, str, str]] = []
    if not isinstance(data, dict):
        return ops
    raw_ops = data.get("ops")
    if not isinstance(raw_ops, list):
        return ops
    for item in raw_ops:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        line = item.get("line")
        if op not in ("replace", "delete") or not isinstance(line, int):
            host.count("dedup_rejected")
            continue
        if line not in editable_by_line:
            host.count("skipped_protected")
            continue
        if op == "delete":
            ops.append((line, "delete", ""))
            continue
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            continue
        text = truncate(raw_text, text_limit(layer))
        if not text or find_banned(text):
            if text:
                host.count("banned_dropped")
            continue
        if normalize_text(text) == normalize_text(editable_by_line[line].text):
            continue
        ops.append((line, "replace", text))
    return ops


async def dedup_doc(host: "Consolidator", layer: str, key: str) -> None:
    """一份文档的去重循环；预算同时是调用次数与迭代轮数上限。"""
    while host.can_use("dedup"):
        doc = host.store.load(layer, key)
        editable = [
            entry
            for entry in doc.entries
            if entry.id is not None and not host.is_protected(layer, key, entry)
        ]
        if len(editable) < 2:
            return
        entries_text = "\n".join(
            f"{entry.line_no}. [{entry.date}] {entry.text}" for entry in editable
        )
        data: Any = await host.ask_json(
            "dedup",
            host.render("dedup.system"),
            host.render("dedup.user", doc=f"{layer}/{key}", entries=entries_text),
        )
        if data is None:
            return
        by_line = {entry.line_no: entry for entry in editable}
        ops = _validate_ops(host, data, by_line, layer)
        if not ops:
            return  # 收敛（或全被守卫丢弃）

        def mutate(fresh: MemoryDoc) -> None:
            fresh_by_line = {entry.line_no: entry for entry in fresh.entries}
            for line, op, text in sorted(ops, key=lambda item: item[0], reverse=True):
                target = fresh_by_line.get(line)
                if target is None:
                    continue
                if op == "replace":
                    target.text = text
                else:
                    fresh.entries = [entry for entry in fresh.entries if entry is not target]

        host.touch(layer, key)
        await host.store.apply(layer, key, mutate)
        state_key = doc_key(layer, key)
        for line, op, text in ops:
            entry = by_line[line]
            entry_id = entry.id or ""
            if op == "replace":
                host.state.refresh_entry(
                    state_key, entry_id, text_digest(text), origin="consolidator"
                )
            else:
                host.state.drop_entry_meta(state_key, entry_id)
                host.state.add_tombstone(entry_id, layer, key)
            host.count("dedup_ops")
        host.note(f"dedup:{layer}/{key} {len(ops)}")


def text_limit(layer: str) -> int:
    return _TEXT_LIMITS.get(layer, 400)
