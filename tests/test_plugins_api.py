"""plugins 内省 API：清单、schema、可用性与双语渲染。"""

import re

from nnnu.runtime import bootstrap
from nnnu.services.settings.service import get_settings_service

# 汉字（U+4E00–U+9FFF）：用来区分「渲染出了中文」和「回退成英文键」
CJK = re.compile("[一-鿿]")


def _by_name(data: dict) -> dict:
    return {item["definition"]["name"]: item for item in data["tools"]}


async def test_plugins_lists_chat_and_ask_user(client):
    bootstrap.register_builtins()
    resp = await client.get("/api/v1/plugins")
    assert resp.status_code == 200
    data = resp.json()
    tool_names = [item["definition"]["name"] for item in data["tools"]]
    assert "ask_user" in tool_names
    ask_user = next(item for item in data["tools"] if item["definition"]["name"] == "ask_user")
    assert ask_user["available"] is True
    assert ask_user["definition"]["mount"] == "always"
    assert "parameters" in ask_user["definition"]
    capability_names = [item["name"] for item in data["capabilities"]]
    assert "chat" in capability_names
    assert "deep_solve" in capability_names
    chat = next(item for item in data["capabilities"] if item["name"] == "chat")
    assert chat["stages"] == []
    assert chat["default_model_role"] == "chat"
    # 三阶段流水线的声明（§7.3）：步骤条与阶段提示词都按这个顺序走
    solve = next(item for item in data["capabilities"] if item["name"] == "deep_solve")
    assert [stage["key"] for stage in solve["stages"]] == ["planning", "reasoning", "writing"]
    assert all(stage["label_i18n"] and stage["max_rounds"] > 0 for stage in solve["stages"])
    assert solve["config_schema"]["mode"]["enum"] == ["full", "hint"]
    # 出题能力（§7.4）：两段 IDEATION → GENERATION，配置项就是出题规格那四项
    question = next(item for item in data["capabilities"] if item["name"] == "deep_question")
    assert [stage["key"] for stage in question["stages"]] == ["ideation", "generation"]
    assert all(stage["label_i18n"] and stage["max_rounds"] > 0 for stage in question["stages"])
    assert set(question["config_schema"]) == {
        "num_questions",
        "types",
        "difficulty",
        "knowledge_point",
    }
    assert question["config_schema"]["types"]["items"]["enum"] == ["single", "multi", "short"]
    assert "question_bank" in tool_names


async def test_availability_follows_prerequisites_not_mount(client, tmp_home):
    """available = 前置条件就绪，跟「本回合挂没挂」无关：没配图像模型就干不了活。"""
    bootstrap.register_builtins()
    tools = _by_name((await client.get("/api/v1/plugins")).json())
    assert tools["ask_user"]["available"] is True
    assert tools["imagegen"]["available"] is False  # image_model 还是空的
    assert tools["videogen"]["available"] is False  # 只有友好报错，没真生成

    get_settings_service().save_area("models", {"image_model": "gpt-image-1"})
    tools = _by_name((await client.get("/api/v1/plugins")).json())
    assert tools["imagegen"]["available"] is True
    assert tools["videogen"]["available"] is False  # 配了图也不代表视频能生成


async def test_descriptions_and_cost_hints_render_both_langs(client, repo_prompts):
    """定义里存的是提示词键，接口按 ?lang= 渲染成文案（§10.2）——声明了键的工具两种语言都得有。

    只查声明了 tools.* 键的工具：测试自己注册的桩工具（描述是写死的中文）不在此列，
    全局 registry 里留下的桩不该把这条契约测崩。
    """
    from nnnu.runtime.registry.tool_registry import get_tool_registry

    bootstrap.register_builtins()
    zh = _by_name((await client.get("/api/v1/plugins?lang=zh")).json())
    en = _by_name((await client.get("/api/v1/plugins?lang=en")).json())
    assert set(zh) == set(en)

    keyed = [d for d in get_tool_registry().definitions() if d.description.startswith("tools.")]
    assert keyed, "没有工具声明提示词键，这条用例就白测了"
    for definition in keyed:
        name = definition.name
        for lang_name, rendered in (("zh", zh), ("en", en)):
            description = rendered[name]["definition"]["description"]
            assert not description.startswith("tools."), f"{name} 的 {lang_name} 说明没渲染出来"
            if lang_name == "zh":
                assert CJK.search(description), f"{name} 的中文说明没渲染出来"
            else:
                assert not CJK.search(description), f"{name} 的英文说明没渲染出来"
        if definition.cost_hint:
            assert CJK.search(zh[name]["definition"]["cost_hint"]), (
                f"{name} 的中文成本提示没渲染出来"
            )
            assert not CJK.search(en[name]["definition"]["cost_hint"]), (
                f"{name} 的英文成本提示没渲染出来"
            )


async def test_tools_without_cost_hint_report_null(client):
    """没写成本提示的工具（如 ask_user）下发 null，前端据此不显示那行。"""
    bootstrap.register_builtins()
    tools = _by_name((await client.get("/api/v1/plugins")).json())
    assert tools["ask_user"]["definition"]["cost_hint"] is None
