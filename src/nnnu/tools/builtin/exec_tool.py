"""exec 工具（§7.2 / §11.2）：本机命令执行。

默认禁用（chat 设置 tools_disabled 默认含 exec，需用户显式开启）；
命令经 shell 执行但工作目录锁定 data/workspace/，超时/输出限制同沙箱。
联网同沙箱：请求恒为"要"（工具没有联网参数），真正放行看设置里的「沙箱允许联网」。
"""

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.sandbox.service import get_sandbox_service
from nnnu.services.sandbox.spec import ExecRequest

SNIPPET_MAX_CHARS = 2000


class ExecTool(BaseTool):
    definition = ToolDefinition(
        name="exec",
        description="tools.exec",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                },
            },
            "required": ["command"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.exec",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        request = ExecRequest(
            command=str(ctx.args.get("command", "")),
            timeout_s=float(ctx.args.get("timeout_seconds", 30)),
            allow_network=True,  # 请求联网；服务层按设置里的总开关决定是否真放行（§11.2）
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
        )
        result = await get_sandbox_service().run(request)
        if result.error:
            output = f"命令执行失败：{result.error}"
            if result.stdout.strip():
                output += f"\n{result.stdout.strip()[:SNIPPET_MAX_CHARS]}"
            return ToolResult(ok=False, output=output)
        text = result.stdout.strip() or "（无输出）"
        return ToolResult(
            ok=True,
            output=text[:SNIPPET_MAX_CHARS],
            detail={"exit_code": result.exit_code, "duration_s": round(result.duration_s, 2)},
        )
