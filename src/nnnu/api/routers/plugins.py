"""插件内省 API（§6.11）：GET /api/v1/plugins——工具/能力清单 + schema + 可用性。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/plugins")
async def list_plugins() -> dict:
    from nnnu.runtime.registry.capability_registry import get_capability_registry
    from nnnu.runtime.registry.tool_registry import get_tool_registry

    tool_registry = get_tool_registry()
    return {
        "tools": [
            {
                "definition": definition.model_dump(),
                "available": tool_registry.get(definition.name) is not None,
            }
            for definition in tool_registry.definitions()
        ],
        "capabilities": [
            manifest.model_dump() for manifest in get_capability_registry().manifests()
        ],
    }
