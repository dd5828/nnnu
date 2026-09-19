"""成本追踪（§6.9，方案 6.7 的 UnifiedContext.cost 字段命名为 CostTracker）。

- 按 (provider, model) 聚合 input/output tokens 与估算费用；
- usage 帧单次记账（流式下只记最后一帧，防多帧重复计）；
- 无 usage 帧回退字符估算（chars/3.5）；
- 未知模型不计费（pricing.lookup_price 返回 None 时 cost 为 0）。
"""

from pydantic import BaseModel

from nnnu.services.cost.pricing import lookup_price

# 字符估算系数（§6.9 无 usage 帧时的回退路径）
CHARS_PER_TOKEN = 3.5


class ModelUsage(BaseModel):
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cost: float = 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CostTracker:
    def __init__(self) -> None:
        self._per_model: dict[str, ModelUsage] = {}

    def add_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """按 usage 帧记账一次；费用按价格表估算，未知模型 cost 为 0。"""
        usage = self._ensure(provider, model)
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.calls += 1
        price = lookup_price(model)
        if price is not None:
            usage.cost += (input_tokens * price[0] + output_tokens * price[1]) / 1000

    def add_estimated(
        self, *, provider: str, model: str, input_chars: int = 0, output_chars: int = 0
    ) -> None:
        """无 usage 帧时的字符回退估算（chars/3.5），同样按价格表计费。"""
        self.add_usage(
            provider=provider,
            model=model,
            input_tokens=round(input_chars / CHARS_PER_TOKEN),
            output_tokens=round(output_chars / CHARS_PER_TOKEN),
        )

    def merge(self, other: "CostTracker") -> None:
        for key, usage in other._per_model.items():
            mine = self._ensure(usage.provider, usage.model)
            mine.input_tokens += usage.input_tokens
            mine.output_tokens += usage.output_tokens
            mine.calls += usage.calls
            mine.cost += usage.cost

    def is_empty(self) -> bool:
        return not self._per_model

    def summary(self) -> dict:
        """§6.9 cost_summary 事件 payload：{tokens, cost, per_model}。"""
        total_tokens = sum(u.tokens for u in self._per_model.values())
        total_cost = round(sum(u.cost for u in self._per_model.values()), 6)
        return {
            "tokens": total_tokens,
            "cost": total_cost,
            "per_model": {key: u.model_dump() for key, u in self._per_model.items()},
        }

    def _ensure(self, provider: str, model: str) -> ModelUsage:
        if model not in self._per_model:
            self._per_model[model] = ModelUsage(provider=provider, model=model)
        return self._per_model[model]
