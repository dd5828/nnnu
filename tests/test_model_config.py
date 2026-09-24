"""模型配置解析（§7.19）：settings > 请求覆盖 > env > 内置默认；密钥槽与 base_url。"""

import pytest

from nnnu.services.llm.errors import LLMConfigError
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.secrets.store import get_secrets_store
from nnnu.services.settings.service import get_settings_service


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    """剥掉宿主机环境里的 NNNU_MODEL，保证测试隔离。"""
    monkeypatch.delenv("NNNU_MODEL", raising=False)


def test_default_without_settings(tmp_home):
    mc = resolve_model_config()
    assert (mc.provider_id, mc.model) == ("deepseek", "deepseek-chat")
    assert mc.base_url == "https://api.deepseek.com/v1"
    assert mc.temperature == 1.0
    assert mc.reasoning_effort is None


def test_settings_override_provider_model(tmp_home):
    get_settings_service().save_area("models", {"provider": "kimi", "model": "moonshot-v1-8k"})
    mc = resolve_model_config()
    assert (mc.provider_id, mc.model) == ("kimi", "moonshot-v1-8k")
    assert mc.base_url == "https://api.moonshot.cn/v1"


def test_settings_api_key_from_secrets(tmp_home):
    store = get_secrets_store()
    store.set_pending("llm", "deepseek", "sk-stored-key-123456")
    store.promote("llm", "deepseek")
    get_settings_service().save_area("models", {"provider": "deepseek", "model": "deepseek-chat"})
    assert resolve_model_config().api_key == "sk-stored-key-123456"


def test_env_fallback_when_provider_empty(tmp_home, monkeypatch):
    monkeypatch.setenv("NNNU_MODEL", "openai:gpt-4o-mini")
    mc = resolve_model_config()
    assert (mc.provider_id, mc.model) == ("openai", "gpt-4o-mini")
    assert mc.base_url == "https://api.openai.com/v1"


def test_request_override_wins_over_settings(tmp_home):
    get_settings_service().save_area("models", {"provider": "kimi", "model": "moonshot-v1-8k"})
    mc = resolve_model_config(override_provider="deepseek", override_model="deepseek-reasoner")
    assert (mc.provider_id, mc.model) == ("deepseek", "deepseek-reasoner")


def test_cross_provider_override_ignores_settings_model(tmp_home):
    """跨 provider 的请求覆盖不能继承设置里的模型名——那是另一个 provider 的模型 ID。"""
    get_settings_service().save_area("models", {"provider": "kimi", "model": "moonshot-v1-8k"})
    mc = resolve_model_config(override_provider="deepseek", override_model="")
    assert (mc.provider_id, mc.model) == ("deepseek", "deepseek-chat")


def test_custom_without_base_url_raises(tmp_home):
    get_settings_service().save_area("models", {"provider": "custom", "model": "my-model"})
    with pytest.raises(LLMConfigError, match="base_url"):
        resolve_model_config()


def test_custom_with_base_url(tmp_home):
    get_settings_service().save_area(
        "models",
        {"provider": "custom", "base_url": "http://192.168.1.5:8000/v1", "model": "my-model"},
    )
    mc = resolve_model_config()
    assert mc.provider_id == "custom"
    assert mc.base_url == "http://192.168.1.5:8000/v1"
    assert mc.model == "my-model"


def test_base_url_override_for_known_provider(tmp_home):
    get_settings_service().save_area(
        "models",
        {"provider": "deepseek", "base_url": "http://proxy.local/v1", "model": "deepseek-chat"},
    )
    mc = resolve_model_config()
    assert mc.base_url == "http://proxy.local/v1"


def test_temperature_and_effort_passthrough(tmp_home):
    get_settings_service().save_area("models", {"temperature": 0.5, "reasoning_effort": "low"})
    mc = resolve_model_config()
    assert mc.temperature == 0.5
    assert mc.reasoning_effort == "low"


def test_model_empty_falls_back_to_provider_default(tmp_home):
    get_settings_service().save_area("models", {"provider": "deepseek"})
    mc = resolve_model_config()
    assert mc.model == "deepseek-chat"


def test_create_client_custom_provider_constructs(tmp_home):
    """custom 不进静态注册表：create_client 用合成 spec，构造成功即通过（无网络 IO）。"""
    client = create_client(
        "my-model",
        provider_id="custom",
        base_url="http://192.168.1.5:8000/v1",
        api_key="sk-custom-key-123",
    )
    assert client is not None


def test_create_client_custom_missing_base_url_raises(tmp_home):
    with pytest.raises(LLMConfigError, match="base_url"):
        create_client("my-model", provider_id="custom")


def test_create_client_custom_missing_key_raises(tmp_home):
    with pytest.raises(LLMConfigError, match="设置页模型卡片"):
        create_client("my-model", provider_id="custom", base_url="http://192.168.1.5:8000/v1")
