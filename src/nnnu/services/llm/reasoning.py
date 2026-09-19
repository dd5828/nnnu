"""推理参数（§6.10）：per-provider thinking 三态映射与温度钳制。

- DeepSeek → extra_body {"thinking": {"type": "enabled"|"disabled"}}；
- Kimi/Moonshot（qwen 系）→ extra_body {"enable_thinking": true|false}；
- 其余兼容端点 → 顶层 reasoning_effort 透传；
- effort 未显式给时按 provider/model 默认表推断；
- 温度超过模型上限时钳制并告警（网关下不崩溃，参考 v1.5.16）。
"""

import logging

from nnnu.services.llm.provider_registry import ModelInfo, ProviderSpec

logger = logging.getLogger(__name__)

# provider/model 默认推理档位（未显式传 effort 时）
DEFAULT_EFFORT_BY_MODEL: dict[str, str] = {
    "deepseek-reasoner": "high",
}


def build_reasoning_kwargs(*, provider_id: str, model: str, reasoning_effort: str | None) -> dict:
    """返回注入 LLM 请求的推理参数 dict（含 extra_body）。"""
    effort = reasoning_effort or DEFAULT_EFFORT_BY_MODEL.get(model)
    if provider_id == "deepseek":
        if effort in (None, "none", "minimal"):
            return {}
        return {"extra_body": {"thinking": {"type": "enabled"}}}
    if provider_id == "kimi":
        if effort in (None, "none"):
            return {}
        return {"extra_body": {"enable_thinking": True}}
    if effort is not None and effort != "none":
        return {"reasoning_effort": effort}
    return {}


def clamp_temperature(temperature: float | None, spec: ProviderSpec, model: str) -> float | None:
    """超过模型温度上限时钳制并告警；未配置上限原样返回。"""
    if temperature is None:
        return None
    info: ModelInfo | None = next((m for m in spec.models if m.id == model), None)
    if info is not None and info.temperature_max is not None and temperature > info.temperature_max:
        logger.warning(
            "温度 %.2f 超过 %s 上限 %.2f，已钳制", temperature, model, info.temperature_max
        )
        return info.temperature_max
    return temperature
