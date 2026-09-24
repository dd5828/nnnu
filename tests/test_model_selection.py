"""聊天模型选择（§6.10）：请求校验、会话级粘性、选项接口（不泄密钥）。"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.secrets.store import get_secrets_store
from nnnu.services.settings.service import get_settings_service

OPTION_FIELDS = {
    "value",
    "provider",
    "provider_label",
    "model",
    "label",
    "context_window",
    "is_local",
    "missing_key",
    "is_active_default",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """剥掉宿主机 NNNU_MODEL 与 .env 兜底密钥，让默认模型与 missing_key 可断言。"""
    monkeypatch.delenv("NNNU_MODEL", raising=False)
    for name in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # 选项接口自己会调 load_dotenv（仓库根 .env 在本机可能真有 key）：换成 no-op
    monkeypatch.setattr("nnnu.services.llm.env.load_dotenv", lambda *args, **kwargs: {})


@pytest.fixture
def scripted():
    """每个 create_client 一份新脚本：多回合测试不必手动续脚本。"""
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["回答"])]))
    yield
    uninstall_scripted()


@pytest.fixture
async def client_app(tmp_home):
    """带 app 句柄的客户端：需要绕过 API 直接改库的用例用它。"""
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


async def _wait_messages(client, session_id: str, count: int) -> dict:
    """regenerate 是后台回合：轮询到消息数到位再断言。"""
    detail: dict = {}
    for _ in range(50):
        detail = (await client.get(f"/api/v1/sessions/{session_id}")).json()
        if len(detail["messages"]) >= count:
            return detail
        await asyncio.sleep(0.05)
    return detail


async def test_model_selection_is_sticky_per_session(client, scripted):
    """带 model 的回合落库；之后的回合不带也沿用（regenerate 不误清）。"""
    chat = await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-model", "message": "问题", "model": "kimi:kimi-k2"},
    )
    assert chat.status_code == 200
    detail = await client.get("/api/v1/sessions/sess-model")
    assert detail.json()["model"] == "kimi:kimi-k2"

    regen = await client.post("/api/v1/sessions/sess-model/regenerate")
    assert regen.status_code == 200
    detail = await _wait_messages(client, "sess-model", 2)
    assert detail["model"] == "kimi:kimi-k2"


async def test_model_selection_explicit_clear(client, scripted):
    """null / 空串都是明确意思：清除选择，回退设置默认。"""
    await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-clear", "message": "一", "model": "openai:gpt-4o"},
    )
    assert (await client.get("/api/v1/sessions/sess-clear")).json()["model"] == "openai:gpt-4o"

    cleared = await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-clear", "message": "二", "model": None},
    )
    assert cleared.status_code == 200
    assert (await client.get("/api/v1/sessions/sess-clear")).json()["model"] is None

    await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-clear", "message": "三", "model": "openai:gpt-4o"},
    )
    blank = await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-clear", "message": "四", "model": "  "},
    )
    assert blank.status_code == 200
    assert (await client.get("/api/v1/sessions/sess-clear")).json()["model"] is None


async def test_model_selection_absent_field_keeps_sticky(client, scripted):
    """老客户端/别处入口压根不带 model 字段：沿用会话现值，不写库。"""
    await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-keep", "message": "一", "model": "openai:gpt-4o"},
    )
    resp = await client.post("/api/v1/chat", json={"session_id": "sess-keep", "message": "二"})
    assert resp.status_code == 200
    assert (await client.get("/api/v1/sessions/sess-keep")).json()["model"] == "openai:gpt-4o"


@pytest.mark.parametrize("bad", ["kimi:", "deepseek-chat", ":x", "gemini:gemini-2.5", 42])
async def test_invalid_model_ref_rejected(client, scripted, bad):
    """选择器只可能产出规范串：其余按坏客户端 fail-fast（422），且不碰会话里的旧值。"""
    await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-bad", "message": "一", "model": "deepseek:deepseek-reasoner"},
    )
    resp = await client.post(
        "/api/v1/chat", json={"session_id": "sess-bad", "message": "二", "model": bad}
    )
    assert resp.status_code == 422
    assert (await client.get("/api/v1/sessions/sess-bad")).json()["model"] == (
        "deepseek:deepseek-reasoner"
    )


async def test_model_name_outside_registry_is_tolerated(client, scripted):
    """注册表是快照不是白名单：兼容端点上自有模型照样能选。"""
    resp = await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-snapshot", "message": "问题", "model": "deepseek:deepseek-v9"},
    )
    assert resp.status_code == 200
    assert (await client.get("/api/v1/sessions/sess-snapshot")).json()["model"] == (
        "deepseek:deepseek-v9"
    )


async def test_stale_stored_model_falls_back(client_app, scripted):
    """库里存的模型已不可用（provider 不在册）：告警丢弃回退设置默认，回合照跑。"""
    client, app = client_app
    marker = ScriptedStep(chunks=["回答"], usage={"prompt_tokens": 3, "completion_tokens": 2})
    install_scripted(lambda: ScriptedLLM([marker]))
    await client.post(
        "/api/v1/chat",
        json={"session_id": "sess-stale", "message": "一", "model": "deepseek:deepseek-reasoner"},
    )
    await app.state.db.execute(
        "UPDATE sessions SET model = ? WHERE id = ?", ("ghost:x", "sess-stale")
    )
    again = await client.post("/api/v1/chat", json={"session_id": "sess-stale", "message": "二"})
    assert again.status_code == 200
    # 回退生效的直接证据：成本按解析后的默认模型记账
    assert list(again.json()["cost_summary"]["per_model"]) == ["deepseek-chat"]


async def test_llm_options_endpoint_shape(client):
    resp = await client.get("/api/v1/settings/llm-options")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] == "deepseek:deepseek-chat"
    assert data["active_source"] == "default"
    # deepseek 2 + kimi 2 + openai 2 + ollama 1；lmstudio/vllm 无内置模型不出行
    assert len(data["options"]) == 7
    assert all(set(option) == OPTION_FIELDS for option in data["options"])
    defaults = [option["value"] for option in data["options"] if option["is_active_default"]]
    assert defaults == ["deepseek:deepseek-chat"]


async def test_llm_options_marks_active_default_from_settings(client):
    get_settings_service().save_area("models", {"provider": "kimi", "model": "moonshot-v1-8k"})
    data = (await client.get("/api/v1/settings/llm-options")).json()
    assert data["active"] == "kimi:moonshot-v1-8k"
    assert data["active_source"] == "settings"
    assert [option["value"] for option in data["options"] if option["is_active_default"]] == [
        "kimi:moonshot-v1-8k"
    ]


async def test_llm_options_marks_active_default_from_env(client, monkeypatch):
    monkeypatch.setenv("NNNU_MODEL", "openai:gpt-4o-mini")
    data = (await client.get("/api/v1/settings/llm-options")).json()
    assert data["active"] == "openai:gpt-4o-mini"
    assert data["active_source"] == "env"


async def test_llm_options_no_secret_leak(client):
    store = get_secrets_store()
    store.set_pending("llm", "deepseek", "sk-secret-should-not-leak")
    store.promote("llm", "deepseek")
    resp = await client.get("/api/v1/settings/llm-options")
    assert "sk-secret-should-not-leak" not in resp.text
    assert "****" not in resp.text  # 掩码也不出本进程
    deepseek_rows = [o for o in resp.json()["options"] if o["provider"] == "deepseek"]
    assert deepseek_rows and all(option["missing_key"] is False for option in deepseek_rows)


async def test_llm_options_missing_key_and_local(client):
    data = (await client.get("/api/v1/settings/llm-options")).json()
    cloud = [option for option in data["options"] if not option["is_local"]]
    local = [option for option in data["options"] if option["is_local"]]
    assert cloud and all(option["missing_key"] for option in cloud)
    assert local and all(option["missing_key"] is False for option in local)


async def test_llm_options_custom_default_only_in_active(client):
    """custom 端点枚举不出来（base_url 即身份）：只作为当前默认出现，不贡献可选项。"""
    get_settings_service().save_area(
        "models",
        {"provider": "custom", "base_url": "http://192.168.1.5:8000/v1", "model": "my-model"},
    )
    data = (await client.get("/api/v1/settings/llm-options")).json()
    assert data["active"] == "custom:my-model"
    assert all(option["provider"] != "custom" for option in data["options"])


async def test_llm_options_unresolved_when_custom_without_base_url(client):
    """配置坏了（custom 缺 base_url）：接口不炸，交给设置页去报错。"""
    get_settings_service().save_area("models", {"provider": "custom", "model": "my-model"})
    resp = await client.get("/api/v1/settings/llm-options")
    assert resp.status_code == 200
    assert resp.json()["active"] is None
    assert resp.json()["active_source"] == "unresolved"


async def test_llm_options_route_not_swallowed_by_area(client):
    """路由顺序回归：声明在 /settings/{area} 之后就会被吞成 area。"""
    assert (await client.get("/api/v1/settings/llm-options")).status_code == 200
    assert (await client.get("/api/v1/settings/nope")).status_code == 404
