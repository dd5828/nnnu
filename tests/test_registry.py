"""注册表与内置注册：幂等、重名替换、挂载视图。"""

import pytest

from nnnu.core.capability_protocol import BaseCapability, CapabilityManifest
from nnnu.core.tool_protocol import (
    BaseTool,
    ToolContext,
    ToolDefinition,
    ToolMount,
    ToolMountFlags,
    ToolResult,
)
from nnnu.runtime import bootstrap
from nnnu.runtime.registry.capability_registry import CapabilityRegistry, get_capability_registry
from nnnu.runtime.registry.tool_registry import ToolRegistry, get_tool_registry


class _EchoTool(BaseTool):
    definition = ToolDefinition(
        name="echo_test", description="回显", parameters={}, mount=ToolMount.ALWAYS
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output="ok")


class _FakeCapability(BaseCapability):
    manifest = CapabilityManifest(name="fake", version="1.0.0")

    async def run(self, ctx, bus) -> None:
        raise NotImplementedError


def test_tool_registry_register_get():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    assert registry.get("echo_test") is not None
    assert registry.definitions()[0].name == "echo_test"


def test_tool_registry_replace_idempotent():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.register(_EchoTool())  # 重复注册替换，不报错
    assert len(registry.definitions()) == 1


def test_tool_registry_strict_duplicate_raises():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ValueError):
        registry.register(_EchoTool(), replace=False)


def test_tool_registry_mounted_view():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    mounted = registry.mounted(ToolMountFlags())
    assert "echo_test" in mounted
    suppressed = registry.mounted(ToolMountFlags(suppressed={"echo_test"}))
    assert "echo_test" not in suppressed


def test_capability_registry_factory_instantiates():
    registry = CapabilityRegistry()
    registry.register(_FakeCapability.manifest, _FakeCapability)
    capability = registry.get("fake")
    assert isinstance(capability, _FakeCapability)
    assert registry.manifests()[0].name == "fake"


def test_builtins_registered_idempotent():
    bootstrap.register_builtins()
    bootstrap.register_builtins()  # 幂等
    tools = get_tool_registry()
    capabilities = get_capability_registry()
    assert tools.get("ask_user") is not None
    assert capabilities.get("chat") is not None
    assert capabilities.get("chat").manifest.stages == []


async def test_ask_user_tool_without_fn_fails_gracefully():
    tool = get_tool_registry().get("ask_user")
    ctx = ToolContext(turn_id="t", session_id="s", args={"question": "q"})
    result = await tool.run(ctx)
    assert result.ok is False
    assert "不支持" in result.output
