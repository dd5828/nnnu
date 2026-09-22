"""次级 LLM 调用助手（§7.2 brainstorm/reason 共用）。

用当前模型配置（resolve_model_config）做一次非流式调用，提示词从
prompts/{lang}/tools.yaml 加载（§5 提示词铁律）；usage 经 metadata 里的
cost_tracker 并入本回合成本（agent_loop 注入，§6.9）。
"""

from typing import Any

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMRequest
from nnnu.services.llm.reasoning import build_reasoning_kwargs


def _usage_tokens(usage: dict[str, Any]) -> tuple[int, int]:
    return (
        int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
    )


async def sub_llm_call(
    ctx: ToolContext,
    prompt_text: str,
    *,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
) -> tuple[str, dict[str, int]]:
    """一次非流式调用；返回 (text, usage)；成本已并入回合。异常向上抛（工具层转 ToolResult）。"""
    mc = resolve_model_config()
    client = create_client(
        mc.model, provider_id=mc.provider_id, base_url=mc.base_url, api_key=mc.api_key
    )
    response = await client.complete(
        LLMRequest(
            messages=[{"role": "user", "content": prompt_text}],
            model=mc.model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            thinking_extra=build_reasoning_kwargs(
                provider_id=mc.provider_id, model=mc.model, reasoning_effort=reasoning_effort
            ),
        )
    )
    if response.usage:
        tracker = ctx.metadata.get("cost_tracker")
        if tracker is not None:
            prompt_tokens, completion_tokens = _usage_tokens(response.usage)
            tracker.add_usage(
                provider=mc.provider_id,
                model=mc.model,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            )
    return response.text.strip(), dict(response.usage)


def render_tool_prompt(key: str, lang: str, **variables: str | int) -> str:
    """tools.yaml 辅助提示词渲染（缺语言回退链由提示词管理器保证）。"""
    return get_prompt_manager().render("tools", lang, key, **variables) or ""
