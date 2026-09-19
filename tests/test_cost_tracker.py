"""成本追踪：价格匹配、估算回退、per_model 聚合与合并。"""

from nnnu.services.cost.pricing import lookup_price
from nnnu.services.cost.tracker import CostTracker


def test_lookup_price_fuzzy_match():
    assert lookup_price("deepseek-chat") == (0.00014, 0.00028)
    assert lookup_price("gpt-4o-2024-08-06") == (0.0025, 0.010)  # 子串命中
    assert lookup_price("claude-3-5-sonnet-20241022") == (0.003, 0.015)


def test_lookup_price_unknown_returns_none():
    assert lookup_price("some-future-model") is None


def test_lookup_price_local_markers_return_none():
    assert lookup_price("ollama:qwen3") is None
    assert lookup_price("lmstudio-community/llama3") is None


def test_add_usage_aggregates_per_model():
    tracker = CostTracker()
    tracker.add_usage(provider="deepseek", model="deepseek-chat", input_tokens=1000, output_tokens=200)
    tracker.add_usage(provider="deepseek", model="deepseek-chat", input_tokens=500, output_tokens=100)
    summary = tracker.summary()
    assert summary["tokens"] == 1800
    per = summary["per_model"]["deepseek-chat"]
    assert per["input_tokens"] == 1500
    assert per["output_tokens"] == 300
    assert per["calls"] == 2
    assert per["provider"] == "deepseek"
    # 0.00014*1500/1000 + 0.00028*300/1000
    assert abs(per["cost"] - (0.00021 + 0.000084)) < 1e-9


def test_unknown_model_zero_cost_but_tokens_counted():
    tracker = CostTracker()
    tracker.add_usage(provider="x", model="mystery-model", input_tokens=1000, output_tokens=0)
    summary = tracker.summary()
    assert summary["tokens"] == 1000
    assert summary["cost"] == 0.0


def test_add_estimated_chars_ratio():
    tracker = CostTracker()
    tracker.add_estimated(provider="deepseek", model="deepseek-chat", input_chars=350, output_chars=35)
    per = tracker.summary()["per_model"]["deepseek-chat"]
    assert per["input_tokens"] == 100
    assert per["output_tokens"] == 10


def test_merge_combines_trackers():
    a = CostTracker()
    b = CostTracker()
    a.add_usage(provider="deepseek", model="deepseek-chat", input_tokens=100, output_tokens=10)
    b.add_usage(provider="deepseek", model="deepseek-chat", input_tokens=200, output_tokens=20)
    b.add_usage(provider="kimi", model="kimi-k2", input_tokens=50, output_tokens=5)
    a.merge(b)
    summary = a.summary()
    assert set(summary["per_model"]) == {"deepseek-chat", "kimi-k2"}
    assert summary["per_model"]["deepseek-chat"]["calls"] == 2


def test_is_empty():
    tracker = CostTracker()
    assert tracker.is_empty()
    tracker.add_usage(provider="deepseek", model="deepseek-chat", input_tokens=1, output_tokens=0)
    assert not tracker.is_empty()
