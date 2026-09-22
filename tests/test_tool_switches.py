"""工具开关（§7.2）：chat 设置与请求级配置合并进挂载修饰。"""

from nnnu.runtime.orchestrator import build_unified_context
from nnnu.runtime.turn_runtime import TurnRequest
from nnnu.services.sessions.models import Message, Session
from nnnu.services.settings.service import get_settings_service


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
