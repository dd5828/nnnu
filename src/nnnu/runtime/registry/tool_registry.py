"""工具注册表（§6.11）：进程全局单例，注册幂等（重名替换）。"""

import logging

from nnnu.core.tool_protocol import BaseTool, ToolDefinition, ToolMountFlags, compute_mounted_tools

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool, *, replace: bool = True) -> None:
        """注册工具；replace=True 重名替换（bootstrap 幂等），False 重名抛错（插件严格加载）。"""
        name = tool.definition.name
        if name in self._tools:
            if not replace:
                raise ValueError(f"工具 {name} 已注册")
            logger.debug("工具 %s 重复注册，已替换", name)
        self._tools[name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def mounted(self, flags: ToolMountFlags) -> dict[str, BaseTool]:
        """应用挂载纯函数（§6.3），返回挂载工具名 → 实例。"""
        names = compute_mounted_tools(self.definitions(), flags)
        return {name: self._tools[name] for name in names if name in self._tools}


_tool_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry
