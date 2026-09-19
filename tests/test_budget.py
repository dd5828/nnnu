"""预算裁剪：最旧工具结果→最旧消息、保留最近 3 轮与 system。"""

from nnnu.core.budget import estimate_tokens, message_tokens, total_tokens, trim_history


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _rounds(count: int, start: int = 0) -> list[dict]:
    """count 轮 (user, tool, assistant) 会话（编号从 start 起）。"""
    messages: list[dict] = []
    for i in range(start, start + count):
        messages += [_msg("user", f"q{i}"), _msg("tool", "结果" * 100), _msg("assistant", f"a{i}")]
    return messages


def test_estimate_tokens_ceil_ratio():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 35) == 10
    assert estimate_tokens("a" * 36) == 11


def test_message_tokens_counts_tool_calls():
    plain = _msg("assistant", "hello")
    with_call = {
        "role": "assistant",
        "content": "hello",
        "tool_calls": [{"name": "add", "arguments": '{"a": 1}'}],
    }
    assert message_tokens(with_call) > message_tokens(plain)
    assert total_tokens([plain, with_call]) == message_tokens(plain) + message_tokens(with_call)


def test_trim_tool_results_first():
    messages = [_msg("system", "sys" * 10)] + _rounds(5)
    trimmed, removed = trim_history(
        messages, budget=total_tokens(messages) // 2, reserve=0, keep_last_rounds=2
    )
    # 老轮次的 tool 结果全删（新 2 轮保护）；工具删完后仍超限才动 user/assistant
    assert removed >= 3
    assert trimmed[0]["role"] == "system"
    # 最近 2 轮（第 4-5 轮）完整保留（尾部 6 条）
    assert trimmed[-6:] == _rounds(2, start=3)
    # 裁剪后不超预算
    assert total_tokens(trimmed) <= total_tokens(messages) // 2


def test_keep_last_3_rounds():
    messages = [_msg("system", "s")] + _rounds(5)
    trimmed, removed = trim_history(messages, budget=1, reserve=0, keep_last_rounds=3)
    # 最近 3 轮（第 3-5 轮，尾部 9 条）完整保留
    assert trimmed[-9:] == _rounds(3, start=2)
    assert trimmed[0]["role"] == "system"
    assert removed == 6


def test_system_never_dropped():
    messages = [
        _msg("system", "s" * 500),
        _msg("user", "q1"),
        _msg("assistant", "a1"),
        _msg("user", "q2"),
    ]
    trimmed, _ = trim_history(messages, budget=1, reserve=0, keep_last_rounds=1)
    assert trimmed[0]["role"] == "system"


def test_over_budget_best_effort():
    messages = [_msg("user", "q1" * 500)]
    trimmed, removed = trim_history(messages, budget=10, reserve=0, keep_last_rounds=3)
    # 删无可删仍超限：尽力返回（保护区间内不删）
    assert len(trimmed) == 1
    assert removed == 0


def test_under_budget_untouched():
    messages = [_msg("system", "s"), _msg("user", "q"), _msg("assistant", "a")]
    trimmed, removed = trim_history(messages, budget=100000, reserve=0)
    assert removed == 0
    assert trimmed == messages
