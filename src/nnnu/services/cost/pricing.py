"""模型价格表（§6.9）：每 1K token 美元价，模糊匹配。

Adapted from DeepTutor (Apache-2.0) deeptutor/logging/stats/llm_stats.py
——数据按方案 §16.4 白名单 C 允许复制，补充 deepseek-reasoner/kimi 与本地 0 价。
与参考实现的差异：未知模型不落 gpt-4o-mini 兜底价，按方案 §6.9「未知模型按
token 数显示不计费」返回 None。价格是快照，后续随市场变动更新。
"""

# 每 1K token 美元价；键为模型子串（模糊匹配）
PRICE_TABLE: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "deepseek-chat": {"input": 0.00014, "output": 0.00028},
    "deepseek-reasoner": {"input": 0.00055, "output": 0.00219},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "kimi-k2": {"input": 0.0006, "output": 0.0025},
    "moonshot-v1": {"input": 0.0006, "output": 0.0025},
}

# 本地端点（Ollama/LM Studio/vLLM）不按 token 计费
LOCAL_MODEL_MARKERS = ("ollama", "lmstudio", "vllm")


def lookup_price(model: str) -> tuple[float, float] | None:
    """按模型名子串双向模糊匹配，返回 (input, output) 每 1K 美元价。

    未知模型或本地端点返回 None（不计费，仅统计 token 数）。
    """
    model_lower = model.lower()
    if any(marker in model_lower for marker in LOCAL_MODEL_MARKERS):
        return None
    for key, pricing in PRICE_TABLE.items():
        if key in model_lower or model_lower in key:
            return pricing["input"], pricing["output"]
    return None
