"""provider 静态注册表：解析优先级与探测。"""

from nnnu.services.llm.provider_registry import (
    build_registry,
    default_model,
    find_model,
    resolve_provider,
)

SPECS = build_registry()


def test_resolve_explicit_id():
    spec = resolve_provider(SPECS, provider_id="kimi")
    assert spec is not None and spec.id == "kimi"


def test_resolve_base_url_keyword_detect():
    spec = resolve_provider(SPECS, base_url="http://localhost:11434/v1")
    assert spec is not None and spec.id == "ollama"
    spec2 = resolve_provider(SPECS, base_url="https://api.deepseek.com/v1")
    assert spec2 is not None and spec2.id == "deepseek"


def test_resolve_key_prefix():
    spec = resolve_provider(SPECS, api_key="sk-proj-abc")
    assert spec is not None and spec.id == "openai"


def test_resolve_none_on_no_match():
    assert resolve_provider(SPECS, base_url="http://unknown:9999/v1") is None


def test_default_model():
    assert default_model(next(s for s in SPECS if s.id == "deepseek")) == "deepseek-chat"
    assert default_model(next(s for s in SPECS if s.id == "lmstudio")) is None


def test_find_model():
    spec = next(s for s in SPECS if s.id == "deepseek")
    info = find_model(spec, "deepseek-chat")
    assert info is not None
    assert info.context_window == 131072
    assert find_model(spec, "nope") is None


def test_local_providers_have_no_key_env():
    for spec in SPECS:
        if spec.is_local:
            assert spec.api_key_env is None
