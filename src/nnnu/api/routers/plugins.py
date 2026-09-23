"""插件内省 API（§6.11）：GET /api/v1/plugins——工具/能力清单 + schema + 可用性。

工具定义里 description / cost_hint 存的是提示词键（见 §10.1 chat.yaml），这里按
?lang= 渲染成实际文案再下发——设置页的工具目录直接显示，不用自己认键。

可用性 = 前置条件是否就绪（模型/配置层面），跟「本回合挂没挂」无关：没配图像模型
就没有可用的 imagegen（§7.2 验收「未配置前置条件的工具不挂载」）。
"""

from fastapi import APIRouter

router = APIRouter()


def _availability(name: str) -> bool:
    """此刻这个工具能不能真干活（读配置判断，不查会话状态）。"""
    if name == "imagegen":
        from nnnu.services.media.service import image_generation_available

        return image_generation_available()
    if name == "videogen":
        return False  # 只有友好提示、没有真生成（§7.2 未交付）
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
        item["description"] = (
            prompts.render("chat", lang, f"tool_descriptions.{definition.name}")
            or definition.description
        )
        # 没写成本提示的工具（值是 None）不查键，免得每次调用都刷「键缺失」告警
        if definition.cost_hint:
            item["cost_hint"] = (
                prompts.render("chat", lang, f"tool_cost_hints.{definition.name}") or None
            )
        tools.append({"definition": item, "available": _availability(definition.name)})

    return {
        "tools": tools,
        "capabilities": [
            manifest.model_dump() for manifest in get_capability_registry().manifests()
        ],
    }
