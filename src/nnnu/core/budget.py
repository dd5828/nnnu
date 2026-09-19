"""上下文预算（§6.6）：token 估算与裁剪纯函数。

- 估算：ceil(len/3.5) 字符系数；工具调用加固定开销；
- 裁剪顺序（nnnu 规则）：先删最旧 tool 结果 → 再删最旧 user/assistant 消息，
  保留 system 提示与最近 keep_last_rounds 轮（轮边界 = user 消息）；
- 删无可删仍超限 → 尽力返回（调用方发 warning）。
"""

import json
import math
from typing import Any

CHARS_PER_TOKEN = 3.5
BASE_MESSAGE_OVERHEAD = 8  # 每条消息的 role/结构固定开销
TOOL_CALL_OVERHEAD = 32  # 每个工具调用的 name/结构开销


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def message_tokens(message: dict[str, Any]) -> int:
    total = BASE_MESSAGE_OVERHEAD + estimate_tokens(str(message.get("content", "")))
    for call in message.get("tool_calls") or []:
        total += TOOL_CALL_OVERHEAD + estimate_tokens(
            str(call.get("name", "")) + str(call.get("arguments", ""))
        )
    return total


def total_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(message) for message in messages)


def _user_boundary(messages: list[dict[str, Any]], keep_last_rounds: int) -> int:
    """保护区间起点：从尾部数 keep_last_rounds 个 user 消息中最早者的下标。

    轮边界 = user 消息；无足够轮数时保护全部（返回 0）。
    """
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) < keep_last_rounds:
        return 0
    return user_indices[-keep_last_rounds]


def trim_history(
    messages: list[dict[str, Any]],
    *,
    budget: int,
    reserve: int,
    keep_last_rounds: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """裁剪到 budget - reserve 以内；返回 (裁剪后消息, 删除条数)。"""
    msgs = list(messages)
    limit = max(0, budget - reserve)
    total = total_tokens(msgs)
    if total <= limit:
        return msgs, 0
    protected_from = _user_boundary(msgs, keep_last_rounds)
    removed = 0

    def _drop(index: int) -> None:
        nonlocal total, protected_from, removed
        total -= message_tokens(msgs[index])
        del msgs[index]
        removed += 1
        # 删除后保护区间起点前移一位（保护内容不变）
        protected_from = max(0, protected_from - 1)

    # Phase1：删最旧 tool 消息（保护区间外）
    index = 0
    while index < len(msgs) and total > limit and index < protected_from:
        if msgs[index].get("role") == "tool":
            _drop(index)
            continue
        index += 1
    # Phase2：删最旧 user/assistant（成对语义逐个删，保护区间外；system 永不删）
    index = 0
    while index < len(msgs) and total > limit and index < protected_from:
        if msgs[index].get("role") in ("user", "assistant"):
            _drop(index)
            continue
        index += 1
    return msgs, removed


def schema_tokens(tools: list[dict[str, Any]]) -> int:
    """工具 schema 预算（每轮请求固定开销，计入 reserve）。"""
    return estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
