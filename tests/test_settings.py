"""设置服务与 API：默认值、合并、即时生效、原子写、宽容加载 + 草稿-应用（§7.19）。"""

import json

import pytest

from nnnu.services.llm.probe import ProbeResult
from nnnu.services.secrets.store import get_secrets_store
from nnnu.services.settings.service import (
    DraftError,
    ProbeError,
    SettingsService,
    get_settings_service,
)

APPEARANCE_DEFAULTS = {"theme": "light", "ui_language": "zh", "output_language": "zh"}


@pytest.fixture
def svc(tmp_home) -> SettingsService:
    return get_settings_service()


def _write_area(tmp_home, area: str, data: dict) -> None:
    path = tmp_home / "data" / "user" / "settings" / f"{area}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_load_defaults_when_file_missing(svc):
    assert svc.load_area("appearance") == APPEARANCE_DEFAULTS


def test_load_merges_over_defaults(svc, tmp_home):
    _write_area(tmp_home, "appearance", {"theme": "dark"})
    assert svc.load_area("appearance") == {**APPEARANCE_DEFAULTS, "theme": "dark"}


def test_file_edit_reflected_immediately(svc, tmp_home):
    _write_area(tmp_home, "appearance", {"theme": "dark"})
    assert svc.load_area("appearance")["theme"] == "dark"
    # 直接改 JSON 文件（不经过服务）→ 下一次 load 立即反映（无缓存）
    _write_area(tmp_home, "appearance", {"theme": "beige"})
    assert svc.load_area("appearance")["theme"] == "beige"


def test_save_writes_valid_json(svc, tmp_home):
    svc.save_area("appearance", {"theme": "glass"})
    path = tmp_home / "data" / "user" / "settings" / "appearance.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "glass"
    assert svc.load_area("appearance")["theme"] == "glass"


def test_save_preserves_unknown_keys(svc, tmp_home):
    _write_area(tmp_home, "appearance", {"theme": "dark", "custom_note": "keep me"})
    svc.save_area("appearance", {"theme": "beige"})
    assert svc.load_area("appearance")["custom_note"] == "keep me"


def test_save_invalid_choice_raises(svc):
    with pytest.raises(ValueError):
        svc.save_area("appearance", {"theme": "neon"})


def test_load_invalid_value_falls_back_to_default(svc, tmp_home):
    _write_area(tmp_home, "appearance", {"theme": "neon"})
    assert svc.load_area("appearance")["theme"] == "light"


def test_load_unknown_area_raises(svc):
    with pytest.raises(KeyError):
        svc.load_area("nope")


def test_seed_missing_only_creates_absent_files(svc, tmp_home):
    svc.seed_missing()
    path = tmp_home / "data" / "user" / "settings" / "appearance.json"
    assert path.is_file()
    # 已有文件不被覆盖
    path.write_text('{"theme": "dark"}', encoding="utf-8")
    svc.seed_missing()
    assert svc.load_area("appearance")["theme"] == "dark"


# ---- API 层 ----


async def test_get_settings_returns_values_and_meta(client):
    resp = await client.get("/api/v1/settings/appearance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["values"]["theme"] == "light"
    theme_field = next(f for f in data["fields"] if f["key"] == "theme")
    assert theme_field["type"] == "choice"
    assert theme_field["effect"] == "instant"
    assert set(theme_field["choices"]) == {"light", "beige", "dark", "glass"}
    assert theme_field["current"] == "light"


async def test_put_settings_roundtrip(client):
    resp = await client.put("/api/v1/settings/appearance", json={"values": {"theme": "dark"}})
    assert resp.status_code == 200
    assert resp.json()["values"]["theme"] == "dark"
    resp = await client.get("/api/v1/settings/appearance")
    assert resp.json()["values"]["theme"] == "dark"


async def test_put_settings_validation_error(client):
    resp = await client.put("/api/v1/settings/appearance", json={"values": {"theme": "neon"}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_unknown_area_returns_404_envelope(client):
    resp = await client.get("/api/v1/settings/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_area"


async def test_network_effect_is_restart(client):
    resp = await client.get("/api/v1/settings/network")
    port_field = next(f for f in resp.json()["fields"] if f["key"] == "backend_port")
    assert port_field["effect"] == "restart"


# ---- 草稿-应用两段式（§7.19） ----


def test_draft_save_load_delete(svc, tmp_home):
    svc.save_draft("appearance", {"theme": "dark"})
    assert svc.load_draft("appearance") == {"theme": "dark"}
    svc.delete_draft("appearance")
    assert svc.load_draft("appearance") is None


async def test_apply_draft_writes_and_clears(svc, tmp_home):
    svc.save_draft("appearance", {"theme": "glass"})
    values = await svc.apply_draft("appearance")
    assert values["theme"] == "glass"
    assert svc.load_draft("appearance") is None
    assert svc.load_area("appearance")["theme"] == "glass"


async def test_apply_without_draft_raises(svc, tmp_home):
    with pytest.raises(DraftError):
        await svc.apply_draft("appearance")


async def test_apply_probe_failure_keeps_everything(svc, tmp_home):
    _write_area(tmp_home, "models", {"provider": "deepseek", "model": "deepseek-chat"})
    svc.save_draft("models", {"provider": "kimi", "model": "moonshot-v1-8k"})

    async def fail_probe(candidate):
        assert candidate["provider"] == "kimi"
        return "端点返回 HTTP 500"

    with pytest.raises(ProbeError):
        await svc.apply_draft("models", probe_fn=fail_probe)
    # 原配置未动、草稿保留（§7.19：失败回滚并提示）
    assert svc.load_area("models")["provider"] == "deepseek"
    assert svc.load_draft("models") == {"provider": "kimi", "model": "moonshot-v1-8k"}


def test_save_draft_secret_goes_to_pending(svc, tmp_home):
    svc.save_draft(
        "models",
        {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-draft-key-12345678"},
    )
    draft = svc.load_draft("models")
    assert draft["api_key"] == "set"  # 草稿只有动作标记
    assert "sk-draft-key" not in json.dumps(draft)
    store = get_secrets_store()
    assert store.get_pending("llm", "deepseek") == "sk-draft-key-12345678"
    assert store.get("llm", "deepseek") is None


async def test_apply_secret_set_promotes_into_secrets_not_json(svc, tmp_home):
    svc.save_draft(
        "models",
        {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-final-key-12345678"},
    )

    async def ok_probe(candidate):
        return None

    await svc.apply_draft("models", probe_fn=ok_probe)
    assert get_secrets_store().get("llm", "deepseek") == "sk-final-key-12345678"
    # 设置 JSON 里没有密钥字段（§5 密钥铁律）
    models_path = tmp_home / "data" / "user" / "settings" / "models.json"
    assert "api_key" not in json.loads(models_path.read_text(encoding="utf-8"))


async def test_apply_secret_clear_removes(svc, tmp_home):
    async def ok_probe(candidate):
        return None

    svc.save_draft("models", {"provider": "deepseek", "api_key": "sk-temp-key-12345678"})
    await svc.apply_draft("models", probe_fn=ok_probe)
    assert get_secrets_store().get("llm", "deepseek") == "sk-temp-key-12345678"
    svc.save_draft("models", {"api_key": "clear"})
    await svc.apply_draft("models", probe_fn=ok_probe)
    assert get_secrets_store().get("llm", "deepseek") is None


def test_delete_draft_clears_pending(svc, tmp_home):
    svc.save_draft("models", {"provider": "deepseek", "api_key": "sk-orphan-123456789"})
    svc.delete_draft("models")
    assert get_secrets_store().get_pending("llm", "deepseek") is None


def test_save_area_rejects_secret_field(svc):
    with pytest.raises(ValueError):
        svc.save_area("models", {"api_key": "sk-nope"})


def test_load_area_excludes_secret(svc, tmp_home):
    _write_area(tmp_home, "models", {"provider": "deepseek", "api_key": "sk-should-not-leak"})
    assert "api_key" not in svc.load_area("models")


def test_float_validation(svc):
    with pytest.raises(ValueError):
        svc.save_area("models", {"temperature": "hot"})
    with pytest.raises(ValueError):
        svc.save_area("models", {"temperature": 3.0})
    assert svc.save_area("models", {"temperature": 0.7})["temperature"] == 0.7


# ---- models 区草稿-应用 API ----


async def _fake_probe_success(base_url, api_key=None, *, timeout=8.0, transport=None):
    return ProbeResult(ok=True, models=["deepseek-chat", "deepseek-reasoner"])


async def _fake_probe_fail(base_url, api_key=None, *, timeout=8.0, transport=None):
    return ProbeResult(ok=False, error="端点返回 HTTP 500，不是可用的 /models 服务")


async def test_models_draft_apply_flow(client, monkeypatch):
    monkeypatch.setattr("nnnu.api.routers.settings.probe_models", _fake_probe_success)
    resp = await client.put(
        "/api/v1/settings/models/draft",
        json={
            "values": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "sk-api-key-12345678",
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["draft"]["api_key"] == "set"
    resp = await client.post("/api/v1/settings/models/apply")
    assert resp.status_code == 200
    assert resp.json()["values"]["provider"] == "deepseek"
    # GET 回显只有掩码，没有明文
    resp = await client.get("/api/v1/settings/models")
    data = resp.json()
    assert data["secrets"]["api_key"] == {"set": True, "masked": "sk-a****5678", "pending": False}
    assert "sk-api-key" not in json.dumps(data)
    # 草稿已清（无草稿返回 200 + null，非 404）
    resp = await client.get("/api/v1/settings/models/draft")
    assert resp.status_code == 200
    assert resp.json()["draft"] is None


async def test_models_apply_probe_failure_keeps_config_and_draft(client, monkeypatch):
    monkeypatch.setattr("nnnu.api.routers.settings.probe_models", _fake_probe_fail)
    resp = await client.put(
        "/api/v1/settings/models/draft",
        json={"values": {"provider": "kimi", "model": "moonshot-v1-8k"}},
    )
    assert resp.status_code == 200
    resp = await client.post("/api/v1/settings/models/apply")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "probe_failed"
    # 原配置未动、草稿保留
    resp = await client.get("/api/v1/settings/models")
    assert resp.json()["values"]["provider"] == ""
    resp = await client.get("/api/v1/settings/models/draft")
    assert resp.status_code == 200
    assert resp.json()["draft"] == {"provider": "kimi", "model": "moonshot-v1-8k"}


async def test_apply_without_draft_returns_409(client):
    resp = await client.post("/api/v1/settings/models/apply")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_draft"


async def test_put_models_secret_rejected(client):
    resp = await client.put("/api/v1/settings/models", json={"values": {"api_key": "sk-x"}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_probe_endpoint_uses_registry_base_url(client, monkeypatch):
    async def fake_probe(base_url, api_key=None, *, timeout=8.0, transport=None):
        assert base_url == "https://api.deepseek.com/v1"
        return ProbeResult(ok=True, models=["deepseek-chat"])

    monkeypatch.setattr("nnnu.api.routers.settings.probe_models", fake_probe)
    resp = await client.post("/api/v1/settings/probe", json={"provider": "deepseek"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "models": ["deepseek-chat"], "error": None}
