"""persona（§7.1）：预设/自定义切换、system prompt 渲染与 API 校验。"""

from nnnu.capabilities.chat.personas import (
    CUSTOM_PERSONA,
    PRESET_PERSONAS,
    is_valid_persona,
    resolve_persona_text,
)
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


def test_preset_ids_and_custom():
    assert PRESET_PERSONAS == ("teacher", "peer", "research_assistant")
    assert is_valid_persona("teacher")
    assert is_valid_persona(CUSTOM_PERSONA)
    assert not is_valid_persona("pirate")
    assert not is_valid_persona("")


def test_resolve_preset_text_bilingual():
    prompts = get_prompt_manager()
    zh = resolve_persona_text(persona_id="teacher", description=None, prompts=prompts, lang="zh")
    en = resolve_persona_text(persona_id="teacher", description=None, prompts=prompts, lang="en")
    assert "老师" in zh
    assert "teacher" in en
    assert zh != en


def test_resolve_custom_uses_description():
    prompts = get_prompt_manager()
    text = resolve_persona_text(
        persona_id=CUSTOM_PERSONA, description="你是一只猫，只回答喵。", prompts=prompts, lang="zh"
    )
    assert text == "你是一只猫，只回答喵。"


def test_resolve_unknown_preset_falls_back_empty():
    prompts = get_prompt_manager()
    assert (
        resolve_persona_text(persona_id="pirate", description=None, prompts=prompts, lang="zh")
        == ""
    )


# ---- API：PATCH /sessions/{id} 的 persona 语义 ----


async def test_patch_session_persona_preset(client):
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]

    resp = await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": "teacher"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["persona"] == "teacher"
    assert body["persona_description"] is None

    # 会话粘性：重新获取仍保持
    detail = await client.get(f"/api/v1/sessions/{session_id}")
    assert detail.json()["persona"] == "teacher"


async def test_patch_session_persona_custom(client):
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    resp = await client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"persona": "custom", "persona_description": "你是苏格拉底式提问者"},
    )
    assert resp.status_code == 200
    assert resp.json()["persona_description"] == "你是苏格拉底式提问者"


async def test_patch_persona_clear_and_validation(client):
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": "peer"})

    # 显式 null 清除
    cleared = await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": None})
    assert cleared.json()["persona"] is None

    # 未知 persona id 拒绝
    bad = await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": "pirate"})
    assert bad.status_code == 422

    # custom 缺描述拒绝
    missing = await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": "custom"})
    assert missing.status_code == 422


async def test_patch_title_only_keeps_persona(client):
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": "peer"})
    renamed = await client.patch(f"/api/v1/sessions/{session_id}", json={"title": "新标题"})
    assert renamed.json()["title"] == "新标题"
    assert renamed.json()["persona"] == "peer"


# ---- 端到端：persona 文本注入 system prompt ----


def _system_prompt_of(llm: ScriptedLLM) -> str:
    return llm.calls[0].messages[0]["content"]


async def test_preset_persona_injected_into_system_prompt(client):
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        created = await client.post("/api/v1/sessions", json={})
        session_id = created.json()["id"]
        await client.patch(f"/api/v1/sessions/{session_id}", json={"persona": "teacher"})
        await client.post("/api/v1/chat", json={"session_id": session_id, "message": "问题"})
    finally:
        uninstall_scripted()
    assert captured is not None
    # 预设文本来自 prompts YAML（teacher 中文描述）
    assert "耐心" in _system_prompt_of(captured)


async def test_custom_persona_injected_into_system_prompt(client):
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        created = await client.post("/api/v1/sessions", json={})
        session_id = created.json()["id"]
        await client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"persona": "custom", "persona_description": "只回答喵"},
        )
        await client.post("/api/v1/chat", json={"session_id": session_id, "message": "问题"})
    finally:
        uninstall_scripted()
    assert captured is not None
    assert "只回答喵" in _system_prompt_of(captured)


async def test_no_persona_leaves_prompt_without_preset_text(client):
    captured: ScriptedLLM | None = None

    def factory() -> ScriptedLLM:
        nonlocal captured
        captured = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        return captured

    install_scripted(factory)
    try:
        await client.post("/api/v1/chat", json={"session_id": "sess-no-persona", "message": "问题"})
    finally:
        uninstall_scripted()
    assert captured is not None
    prompt = _system_prompt_of(captured)
    assert "老师" not in prompt and "同伴" not in prompt and "研究助理" not in prompt
