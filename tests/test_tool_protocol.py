"""工具协议：挂载纯函数（always/context_gated/forced/suppressed）与定义模型。"""

import pytest

from nnnu.core.tool_protocol import (
    ToolDefinition,
    ToolMount,
    ToolMountFlags,
    ToolResult,
    compute_mounted_tools,
)


def _def(name: str, mount: ToolMount) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"desc {name}", parameters={}, mount=mount)


def test_mount_always_included():
    tools = [_def("ask_user", ToolMount.ALWAYS), _def("web_search", ToolMount.USER_TOGGLEABLE)]
    assert compute_mounted_tools(tools, ToolMountFlags()) == ["ask_user"]


def test_mount_context_gated_hit_and_miss():
    tools = [_def("rag", ToolMount.CONTEXT_GATED), _def("exec", ToolMount.CONTEXT_GATED)]
    flags = ToolMountFlags(context={"rag"})
    assert compute_mounted_tools(tools, flags) == ["rag"]


def test_forced_overrides_and_unknown_ignored():
    tools = [_def("web_search", ToolMount.USER_TOGGLEABLE)]
    flags = ToolMountFlags(forced={"web_search", "not_a_tool"})
    # 未知名 forced 忽略（fail-closed，不产生幻觉工具）
    assert compute_mounted_tools(tools, flags) == ["web_search"]


def test_suppressed_beats_forced_and_always():
    tools = [_def("ask_user", ToolMount.ALWAYS), _def("web_search", ToolMount.USER_TOGGLEABLE)]
    flags = ToolMountFlags(suppressed={"ask_user"}, forced={"ask_user", "web_search"})
    assert compute_mounted_tools(tools, flags) == ["web_search"]


def test_mount_order_follows_registration():
    tools = [_def("b", ToolMount.ALWAYS), _def("a", ToolMount.ALWAYS)]
    assert compute_mounted_tools(tools, ToolMountFlags()) == ["b", "a"]


def test_tool_result_model_defaults():
    result = ToolResult(ok=True, output="done")
    assert result.detail is None
    assert result.usage is None


def test_tool_definition_openai_schema():
    definition = ToolDefinition(
        name="add",
        description="tools.add",
        parameters={"type": "object", "properties": {"a": {"type": "integer"}}},
    )
    schema = definition.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "add"
    assert schema["function"]["parameters"]["type"] == "object"


def test_tool_definition_invalid_mount():
    with pytest.raises(ValueError):
        ToolDefinition(name="x", description="d", mount="sometimes")  # type: ignore[arg-type]
