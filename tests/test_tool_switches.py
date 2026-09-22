"""工具开关（§7.2）：chat 设置与请求级配置合并进挂载修饰。"""

from nnnu.core.tool_protocol import (
    ToolDefinition,
    ToolMount,
    compute_mounted_tools,
)
from nnnu.runtime.orchestrator import build_unified_context
from nnnu.runtime.turn_runtime import TurnRequest
from nnnu.services.sessions.models import Message, Session
from nnnu.services.settings.service import get_settings_service


def _def(name: str, mount: ToolMount) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"desc {name}", parameters={}, mount=mount)


async def test_tool_switches_merge_from_settings(tmp_home):
    get_settings_service().save_area(
        "chat", {"tools_enabled": ["web_search"], "tools_disabled": ["exec", "imagegen"]}
    )
    request = TurnRequest(message="hi")
    session = Session(id="sess-1", title="t")
    user_message = Message(id="msg-1", session_id="sess-1", role="user", content="hi")
    ctx = build_unified_context(request, session, user_message, [user_message], language="zh")
    assert "web_search" in ctx.tool_flags.forced
    assert {"exec", "imagegen"} <= ctx.tool_flags.suppressed


async def test_exec_suppressed_by_default(tmp_home):
    """§11.2：exec 默认禁用（chat 区默认 tools_disabled 含 exec）。"""
    request = TurnRequest(message="hi")
    session = Session(id="sess-1", title="t")
    user_message = Message(id="msg-1", session_id="sess-1", role="user", content="hi")
    ctx = build_unified_context(request, session, user_message, [user_message], language="zh")
    assert "exec" in ctx.tool_flags.suppressed


async def test_default_turn_mounts_toggleable_tools_but_not_exec(tmp_home):
    """缺省状态开箱可用：可开关工具挂上，exec 不挂（§11.2）；无 KB 的 rag 也不挂。"""
    request = TurnRequest(message="hi")
    session = Session(id="sess-1", title="t")
    user_message = Message(id="msg-1", session_id="sess-1", role="user", content="hi")
    ctx = build_unified_context(request, session, user_message, [user_message], language="zh")
    tools = [
        _def("ask_user", ToolMount.ALWAYS),
        _def("web_search", ToolMount.USER_TOGGLEABLE),
        _def("exec", ToolMount.USER_TOGGLEABLE),
        _def("rag", ToolMount.CONTEXT_GATED),
    ]
    assert compute_mounted_tools(tools, ctx.tool_flags) == ["ask_user", "web_search"]
