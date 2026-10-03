"""工具开关（§7.2）：chat 设置与请求级配置合并进挂载修饰。"""

from nnnu.core.context import UnifiedContext
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


def _turn_ctx(
    message: str = "hi", *, kb_ids: list[str] | None = None, capability: str = "chat"
) -> UnifiedContext:
    """缺省设置下的回合上下文（设置文件不存在即全默认；kb_ids 不传就当没选库）。"""
    request = TurnRequest(message=message, kb_ids=kb_ids or [], capability=capability)
    session = Session(id="sess-1", title="t")
    user_message = Message(id="msg-1", session_id="sess-1", role="user", content=message)
    return build_unified_context(request, session, user_message, [user_message], language="zh")


async def test_tool_switches_merge_from_settings(tmp_home):
    get_settings_service().save_area(
        "chat", {"tools_enabled": ["web_search"], "tools_disabled": ["exec", "imagegen"]}
    )
    ctx = _turn_ctx()
    assert "web_search" in ctx.tool_flags.forced
    assert {"exec", "imagegen"} <= ctx.tool_flags.suppressed


async def test_exec_suppressed_by_default(tmp_home):
    """§11.2：exec 默认禁用（chat 区默认 tools_disabled 含 exec）。"""
    ctx = _turn_ctx()
    assert "exec" in ctx.tool_flags.suppressed


async def test_default_turn_mounts_toggleable_tools_but_not_exec(tmp_home):
    """缺省状态开箱可用：可开关工具挂上，exec 不挂（§11.2）；没选库的 rag 也不挂（§7.9）。"""
    ctx = _turn_ctx()
    tools = [
        _def("ask_user", ToolMount.ALWAYS),
        _def("web_search", ToolMount.USER_TOGGLEABLE),
        _def("exec", ToolMount.USER_TOGGLEABLE),
        _def("rag", ToolMount.CONTEXT_GATED),
    ]
    assert compute_mounted_tools(tools, ctx.tool_flags) == ["ask_user", "web_search"]


async def test_rag_mounts_only_when_kb_selected(tmp_home):
    """§7.9：rag 跟着用户选择走——选了库才命中上下文门，默认不检索知识库。"""
    tools = [_def("rag", ToolMount.CONTEXT_GATED)]
    assert compute_mounted_tools(tools, _turn_ctx().tool_flags) == []
    assert compute_mounted_tools(tools, _turn_ctx(kb_ids=["kb-1"]).tool_flags) == ["rag"]
    # 取消选择（空列表）就是不挂——不是"搜全部库"
    assert compute_mounted_tools(tools, _turn_ctx(kb_ids=[]).tool_flags) == []


def test_cron_mounts_with_zero_jobs(tmp_home):
    """cron 默认挂载（P3 补课）：任务的唯一入口就是 cron 工具本身，
    按"有定时任务才挂"会死锁在零任务状态，模型永远看不到它。"""
    from nnnu.tools.builtin.cron_tool import CronTool

    assert CronTool.definition.mount is ToolMount.USER_TOGGLEABLE
    assert compute_mounted_tools([CronTool.definition], _turn_ctx().tool_flags) == ["cron"]
    # 不想用就照常放进禁用列表（与 exec 同一条通路）
    get_settings_service().save_area("chat", {"tools_disabled": ["exec", "cron"]})
    assert compute_mounted_tools([CronTool.definition], _turn_ctx().tool_flags) == []


async def test_mastery_tools_mount_only_in_mastery_capability(tmp_home):
    """§7.5：学习路径八件只在 mastery_path 的回合挂。它们曾经是普通可开关工具、
    默认挂进每个能力——聊天里模型拿 mastery_status 当"开局第一步"，用户随口问
    个概念就被推销"要不要建学习路径"（2026-10-03 修的 bug）。"""
    from nnnu.tools.builtin.mastery import MASTERY_TOOLS

    definitions = [tool.definition for tool in MASTERY_TOOLS]
    assert compute_mounted_tools(definitions, _turn_ctx().tool_flags) == []
    mastery_flags = _turn_ctx(capability="mastery_path").tool_flags
    assert set(compute_mounted_tools(definitions, mastery_flags)) == {
        definition.name for definition in definitions
    }
    # 用户禁用照旧优先（suppressed 压过上下文门）
    get_settings_service().save_area("chat", {"tools_disabled": ["mastery_build"]})
    mounted = compute_mounted_tools(definitions, _turn_ctx(capability="mastery_path").tool_flags)
    assert "mastery_build" not in mounted
    assert len(mounted) == len(definitions) - 1
