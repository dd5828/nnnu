"""Book 的单次 LLM 调用助手（spine / 块生成 / 页聊天共用）。

不走会话也不走 agent 循环：`resolve_model_config()` 取当前模型配置做一次非流式
调用（照 tools/builtin/sub_llm.py 手法），用量进 `CostTracker`（调用方负责落
usage_records）。模型输出常把 JSON 包在 ``` 围栏或散文里，`extract_json()` 做
确定性的剥离与解析；洗不出 JSON 抛 `BookOutputError`（路由按 502，照 co_writer
先例）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from nnnu.services.cost.tracker import CostTracker
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.protocol import LLMRequest
from nnnu.services.llm.reasoning import build_reasoning_kwargs


class BookOutputError(LLMError):
    """模型这轮没给出可用的输出（解析/校验过不去）——路由按 502。"""


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.DOTALL)


def strip_fences(text: str) -> str:
    """剥一层 markdown 代码围栏（LLM 常把 JSON 包在 ```json 里）。"""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def extract_json(text: str) -> Any:
    """从模型输出里取第一个 JSON 对象/数组；取不到抛 BookOutputError。"""
    body = strip_fences(text)
    starts = [index for index in (body.find("{"), body.find("[")) if index >= 0]
    if not starts:
        raise BookOutputError("模型输出里没有 JSON")
    start = min(starts)
    end = max(body.rfind("}"), body.rfind("]"))
    if end <= start:
        raise BookOutputError("模型输出的 JSON 不完整")
    try:
        return json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise BookOutputError(f"模型输出的 JSON 解析失败：{exc}") from exc


@dataclass(slots=True)
class OneShotResult:
    text: str
    model: str
    usage: dict[str, Any]  # 原始 usage 帧（脚本/真实客户端都可能为空 dict）


async def one_shot(
    *,
    system: str,
    user: str,
    max_tokens: int,
    tracker: CostTracker | None = None,
) -> OneShotResult:
    """一次非流式调用；给了 tracker 就把 usage 记进去（未知模型 cost 为 0）。"""
    config = resolve_model_config()
    client = create_client(
        config.model,
        provider_id=config.provider_id,
        base_url=config.base_url,
        api_key=config.api_key,
    )
    response = await client.complete(
        LLMRequest(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=config.model,
            temperature=config.temperature,
            max_tokens=max_tokens,
            reasoning_effort=config.reasoning_effort,
            thinking_extra=build_reasoning_kwargs(
                provider_id=config.provider_id or "",
                model=config.model,
                reasoning_effort=config.reasoning_effort,
            ),
        )
    )
    if tracker is not None and response.usage:
        tracker.add_usage(
            provider=config.provider_id or "",
            model=config.model,
            input_tokens=int(
                response.usage.get("prompt_tokens") or response.usage.get("input_tokens") or 0
            ),
            output_tokens=int(
                response.usage.get("completion_tokens") or response.usage.get("output_tokens") or 0
            ),
        )
    return OneShotResult(text=response.text.strip(), model=config.model, usage=dict(response.usage))
