"""LLM 客户端工厂（§6.10 / §12.1）：注入点与 provider 解析的唯一入口。

选择优先级（编排器/循环无感知）：
1. install_scripted() 注入（测试：每测试独享脚本）；
2. env NNNU_LLM_MOCK=scripted + NNNU_LLM_SCRIPT（进程级 YAML 脚本，
   供 dev.py 演示与验收②离线预演）→ 进程内单例；
3. 真实：resolve_provider → OpenAICompatClient。

模型解析："provider:model" 前缀格式或显式参数；密钥仅取环境变量（.env 兜底），
永不落配置 JSON；非本地端点缺 key 抛 LLMConfigError。
"""

import logging
import os
from typing import Callable

from nnnu.services.llm.env import load_dotenv
from nnnu.services.llm.errors import LLMConfigError
from nnnu.services.llm.openai_compat import OpenAICompatClient
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.provider_registry import (
    ProviderSpec,
    build_registry,
    default_model,
    find_model,
    resolve_provider,
)
from nnnu.services.llm.scripted import ScriptedLLM

logger = logging.getLogger(__name__)

_scripted_factory: Callable[[], ScriptedLLM] | None = None
_env_scripted: ScriptedLLM | None = None


def install_scripted(factory_fn: Callable[[], ScriptedLLM]) -> None:
    """测试注入：每次 create_client 调用 factory_fn 取新脚本实例。"""
    global _scripted_factory
    _scripted_factory = factory_fn


def uninstall_scripted() -> None:
    global _scripted_factory
    _scripted_factory = None


def parse_model_ref(model: str) -> tuple[str | None, str]:
    """'provider:model' → (provider, model)；无前缀 → (None, 原串)。"""
    if ":" in model:
        provider, _, rest = model.partition(":")
        return provider, rest
    return None, model


def resolve_spec_and_model(
    *,
    provider_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[ProviderSpec, str]:
    """解析 provider spec 与模型名；解析失败抛 LLMConfigError（带指引）。"""
    specs = build_registry()
    spec = resolve_provider(specs, provider_id=provider_id, base_url=base_url, api_key=api_key)
    if spec is None:
        raise LLMConfigError(
            "无法识别 LLM provider。请显式设置 NNNU_MODEL=provider:model"
            f"（已知 provider：{[s.id for s in specs]}）或 NNNU_PROVIDER/base_url"
        )
    if model is None:
        model = default_model(spec) or ""
    if not model:
        raise LLMConfigError(f"provider {spec.id} 未声明模型，请显式指定模型名")
    if provider_id is not None and find_model(spec, model) is None and spec.models:
        logger.warning("模型 %s 不在 %s 内置目录，按兼容端点直连", model, spec.id)
    return spec, model


def _resolve_api_key(spec: ProviderSpec, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if spec.api_key_env:
        return os.environ.get(spec.api_key_env)
    return None


def create_client(
    model: str,
    *,
    provider_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> LLMClient:
    """工厂入口；temperature/reasoning_effort 走 LLMRequest 逐请求传（P2 起缓存）。"""
    global _env_scripted
    if _scripted_factory is not None:
        return _scripted_factory()
    if os.environ.get("NNNU_LLM_MOCK") == "scripted":
        if _env_scripted is None:
            script_path = os.environ.get("NNNU_LLM_SCRIPT")
            if not script_path:
                raise LLMConfigError("NNNU_LLM_MOCK=scripted 需设置 NNNU_LLM_SCRIPT 指向 YAML 脚本")
            from pathlib import Path

            _env_scripted = ScriptedLLM.from_yaml(Path(script_path))
        return _env_scripted

    load_dotenv()
    parsed_provider, parsed_model = parse_model_ref(model)
    provider_id = provider_id or parsed_provider
    model = parsed_model
    spec, resolved_model = resolve_spec_and_model(
        provider_id=provider_id, base_url=base_url, api_key=api_key, model=model
    )
    resolved_key = _resolve_api_key(spec, api_key)
    if resolved_key is None and not spec.is_local:
        raise LLMConfigError(
            f"provider {spec.id} 缺少 API key：请设置环境变量 {spec.api_key_env}"
            "（或仓库根 .env 开发兜底），密钥永不写入设置 JSON"
        )
    client = OpenAICompatClient(spec, resolved_model, api_key=resolved_key)
    logger.info("LLM 客户端就绪 provider=%s model=%s", spec.id, resolved_model)
    return client
