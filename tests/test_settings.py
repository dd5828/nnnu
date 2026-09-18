"""设置服务与 API：默认值、合并、即时生效、原子写、宽容加载。"""

import json

import pytest

from nnnu.services.settings.service import SettingsService, get_settings_service

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
