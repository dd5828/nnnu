"""LLM 客户端工厂（§6.10 / §12.1）：注入点与 provider 解析的唯一入口。

选择优先级（编排器/循环无感知）：
1. install_scripted() 注入（测试：每测试独享脚本）；
2. env NNNU_LLM_MOCK=scripted + NNNU_LLM_SCRIPT（进程级 YAML 脚本，
   供 dev.py 演示与验收②离线预演）→ 进程内单例；
3. 真实：resolve_model_config（settings > env > 内置默认）→ OpenAICompatClient。

密钥：user-secrets（§7.19 草稿-应用落库）> 环境变量（.env 兜底），
永不落配置 JSON；非本地端点缺 key 抛 LLMConfigError。
"""

import logging
import os
from dataclasses import dataclass
from typing import Callable

from nnnu.services.llm.env import load_dotenv
from nnnu.services.llm.errors import LLMConfigError
from nnnu.services.llm.openai_compat import OpenAICompatClient
from nnnu.services.llm.protocol import LLMClient
from nnnu.services.llm.provider_registry import (
    ProviderSpec,
    build_registry,
    default_model,
    find_by_id,
    find_model,
    resolve_provider,
)
from nnnu.services.llm.scripted import ScriptedLLM

logger = logging.getLogger(__name__)

DEFAULT_MODEL_REF = "deepseek:deepseek-chat"  # 未配置时的内置默认（env NNNU_MODEL 可覆盖）


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """一回合模型接入的完整参数（§7.19 模型卡片应用产物）。"""

    provider_id: str
    model: str
    base_url: str | None
    api_key: str | None
    temperature: float
    reasoning_effort: str | None


_scripted_factory: Callable[[], LLMClient] | None = None
_env_scripted: ScriptedLLM | None = None


def install_scripted(factory_fn: Callable[[], LLMClient]) -> None:
    """测试注入：每次 create_client 调用 factory_fn 取新脚本实例（§12.1）。"""
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


def normalize_model_ref(ref: str | None) -> str | None:
    """严格规范化 'provider:model'（§6.10 会话级模型选择）：provider ∈ 内置注册表
    ∪ {custom} 且 model 非空 → 规范串；否则 None（调用方决定拒绝还是回退设置默认）。

    宽松解析（env NNNU_MODEL、设置回显、会话里的历史值）仍走 parse_model_ref——
    不认识的 provider 在这里返回 None，由调用方选择告警丢弃而不是当场炸。
    """
    provider, _, model = (ref or "").partition(":")
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return None
    if provider != "custom" and find_by_id(build_registry(), provider) is None:
        return None
    return f"{provider}:{model}"


def resolve_model_config(
    *,
    override_provider: str | None = None,
    override_model: str | None = None,
) -> ModelConfig:
    """模型配置解析（§7.19）：settings models 区 > 请求覆盖 > env NNNU_MODEL > 内置默认。

    - settings 里 provider 为空 = 用户没配过 → 走 P1 的 env/默认回退；
    - base_url 留空用注册表默认，custom 必须填；
    - 密钥：user-secrets（按 provider 分槽）> spec 声明的环境变量；本地 provider 免密钥。
    """
    from nnnu.services.secrets.store import get_secrets_store
    from nnnu.services.settings.service import get_settings_service

    values = get_settings_service().load_area("models")
    settings_provider = str(values.get("provider") or "")
    settings_model = str(values.get("model") or "")
    provider_id = override_provider or settings_provider
    # 请求覆盖换 provider 时，模型只能来自 override_model（或该 provider 的默认模型）：
    # 设置里的模型名属于另一个 provider，继承过来会把两边的模型 ID 张冠李戴
    model = override_model or (settings_model if provider_id == settings_provider else "")
    base_url = str(values.get("base_url") or "").strip() or None
    temperature = float(values.get("temperature") or 1.0)
    reasoning_effort = str(values.get("reasoning_effort") or "") or None

    specs = build_registry()
    spec: ProviderSpec | None
    if provider_id:
        if provider_id == "custom":
            if not base_url:
                raise LLMConfigError("自定义端点必须填 base_url（设置页模型卡片）")
            spec = ProviderSpec(id="custom", label="自定义端点", base_url=base_url)
        else:
            spec = find_by_id(specs, provider_id)
            if spec is None:
                raise LLMConfigError(f"未知 provider {provider_id}")
            if base_url is None:
                base_url = spec.base_url
        model = model or default_model(spec) or ""
        if not model:
            raise LLMConfigError(f"provider {provider_id} 没有内置模型，请在设置页填写模型名")
    else:
        # 未显式配置 provider：env NNNU_MODEL → 内置默认（P1 行为）
        env_provider, env_model = parse_model_ref(os.environ.get("NNNU_MODEL") or DEFAULT_MODEL_REF)
        spec = find_by_id(specs, env_provider or "")
        if spec is None:
            raise LLMConfigError(f"无法识别 provider {env_provider}，请检查 NNNU_MODEL 环境变量")
        provider_id = spec.id
        model = model or env_model or default_model(spec) or ""
        if not model:
            raise LLMConfigError(f"provider {spec.id} 未声明模型，请显式指定模型名")
        base_url = spec.base_url

    api_key = get_secrets_store().get("llm", provider_id)
    if api_key is None and spec.api_key_env:
        api_key = os.environ.get(spec.api_key_env)
    return ModelConfig(
        provider_id=provider_id,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )


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
    if provider_id == "custom":
        # 自定义端点不进静态注册表（§7.19）：base_url 即身份，spec 现场合成
        if not base_url:
            raise LLMConfigError("自定义端点必须填 base_url（设置页模型卡片）")
        spec = ProviderSpec(id="custom", label="自定义端点", base_url=base_url)
        resolved_model = model
    else:
        spec, resolved_model = resolve_spec_and_model(
            provider_id=provider_id, base_url=base_url, api_key=api_key, model=model
        )
    resolved_key = _resolve_api_key(spec, api_key)
    if resolved_key is None and not spec.is_local:
        if spec.api_key_env:
            raise LLMConfigError(
                f"provider {spec.id} 缺少 API key：请设置环境变量 {spec.api_key_env}"
                "（或仓库根 .env 开发兜底），密钥永不写入设置 JSON"
            )
        raise LLMConfigError(
            f"provider {spec.id} 缺少 API key：请在设置页模型卡片填写（密钥永不写入设置 JSON）"
        )
    client = OpenAICompatClient(spec, resolved_model, api_key=resolved_key)
    logger.info("LLM 客户端就绪 provider=%s model=%s", spec.id, resolved_model)
    return client
