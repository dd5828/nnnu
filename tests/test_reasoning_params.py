"""推理参数：thinking 三态映射与温度钳制。"""

from nnnu.services.llm.provider_registry import build_registry, find_by_id
from nnnu.services.llm.reasoning import build_reasoning_kwargs, clamp_temperature

SPECS = build_registry()


def test_deepseek_thinking_extra_body():
    kwargs = build_reasoning_kwargs(
        provider_id="deepseek", model="deepseek-chat", reasoning_effort="high"
    )
    assert kwargs == {"extra_body": {"thinking": {"type": "enabled"}}}
    # effort 未给但模型默认表命中
    kwargs2 = build_reasoning_kwargs(
        provider_id="deepseek", model="deepseek-reasoner", reasoning_effort=None
    )
    assert kwargs2 == {"extra_body": {"thinking": {"type": "enabled"}}}


def test_deepseek_none_effort_no_thinking_param():
    assert (
        build_reasoning_kwargs(
            provider_id="deepseek", model="deepseek-chat", reasoning_effort="none"
        )
        == {}
    )
    assert (
        build_reasoning_kwargs(provider_id="deepseek", model="deepseek-chat", reasoning_effort=None)
        == {}
    )


def test_kimi_enable_thinking():
    kwargs = build_reasoning_kwargs(provider_id="kimi", model="kimi-k2", reasoning_effort="high")
    assert kwargs == {"extra_body": {"enable_thinking": True}}
    assert (
        build_reasoning_kwargs(provider_id="kimi", model="kimi-k2", reasoning_effort="none") == {}
    )


def test_other_provider_top_level_effort():
    kwargs = build_reasoning_kwargs(provider_id="openai", model="gpt-4o", reasoning_effort="medium")
    assert kwargs == {"reasoning_effort": "medium"}
    assert (
        build_reasoning_kwargs(provider_id="openai", model="gpt-4o", reasoning_effort="none") == {}
    )


def test_temperature_clamp():
    spec = find_by_id(SPECS, "deepseek")
    # deepseek-chat 上限 1.5
    assert clamp_temperature(2.0, spec, "deepseek-chat") == 1.5
    assert clamp_temperature(1.0, spec, "deepseek-chat") == 1.0
    assert clamp_temperature(None, spec, "deepseek-chat") is None
    # 无上限模型原样
    assert clamp_temperature(2.0, spec, "deepseek-reasoner") == 2.0
