"""插件内省 API（§6.11）：GET /api/v1/plugins——工具/能力清单 + schema + 可用性。

工具定义里 description / cost_hint 存的是提示词键（见 §10.1 chat.yaml），这里按
?lang= 渲染成实际文案再下发——设置页的工具目录直接显示，不用自己认键。

可用性 = 前置条件是否就绪（模型/配置层面），跟「本回合挂没挂」无关：没配图像模型
就没有可用的 imagegen（§7.2 验收「未配置前置条件的工具不挂载」）。
"""

from fastapi import APIRouter

router = APIRouter()


async def _availability(name: str) -> bool:
    """此刻这个工具能不能真干活（读配置/库判断，不查会话状态）。

    videogen 不在此列：它压根不注册（见 bootstrap.register_builtins），
    挂上去只有友好报错、没有真生成（§7.2 未交付）。
    """
    if name == "imagegen":
        from nnnu.services.media.service import image_generation_available

        return image_generation_available()
    if name in ("read_skill", "load_tools"):
        # 技能两件是 CONTEXT_GATED：有启用技能才挂得上（§7.16）
        from nnnu.services.skills.service import get_skill_service

        try:
            return bool(await get_skill_service().enabled_names())
        except RuntimeError:
            return False
    if name in ("list_cli_apps", "run_cli_app"):
        # 命令行应用两件是 CONTEXT_GATED：装了至少一件才挂得上（§7.18）
        from nnnu.services.cli_apps.service import get_cli_app_service

        try:
            return bool(get_cli_app_service().installed_ids())
        except Exception:  # 目录读不出来时如实报「此刻干不了活」
            return False
    if name.startswith("mcp_"):
        # MCP 工具（§7.17）：只有连接活着才有适配器。断连的服务器在注册表里已经
        # 没有它的工具，但配置里可能还列着——按「此刻能不能真干活」的口径报 false。
        from nnnu.services.mcp.manager import get_mcp_manager

        return any(adapter.definition.name == name for adapter in get_mcp_manager().tools())
    return True


@router.get("/api/v1/plugins")
async def list_plugins(lang: str = "zh") -> dict:
    from nnnu.runtime.registry.capability_registry import get_capability_registry
    from nnnu.runtime.registry.tool_registry import get_tool_registry
    from nnnu.services.i18n.prompts import get_prompt_manager

    prompts = get_prompt_manager()
    tool_registry = get_tool_registry()

    tools: list[dict] = []
    for definition in tool_registry.definitions():
        item = definition.model_dump()
        # 先问键在不在：MCP 工具的描述由服务器给，chat.yaml 里没有它的键——
        # 不问就渲染会把「键缺失」告警刷满日志（描述本身照样要下发）
        desc_key = f"tool_descriptions.{definition.name}"
        if prompts.has_key("chat", lang, desc_key):
            item["description"] = prompts.render("chat", lang, desc_key) or definition.description
        # 没写成本提示的工具（值是 None）不查键，免得每次调用都刷「键缺失」告警
        if definition.cost_hint and prompts.has_key(
            "chat", lang, f"tool_cost_hints.{definition.name}"
        ):
            item["cost_hint"] = (
                prompts.render("chat", lang, f"tool_cost_hints.{definition.name}") or None
            )
        tools.append({"definition": item, "available": await _availability(definition.name)})

    return {
        "tools": tools,
        "capabilities": [
            manifest.model_dump() for manifest in get_capability_registry().manifests()
        ],
    }
