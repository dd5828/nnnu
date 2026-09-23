"""code_execution 工具（§7.2）：沙箱化 Python 执行，回传 stdout/exit code。

模型写代码（NL 意图 → 代码由模型完成），本工具只负责安全执行；
执行环境锁在 data/workspace/，超时/输出上限/环境白名单见 services/sandbox。
联网默认关闭：allow_network 只是"请求"，总开关在设置 chat 区 sandbox_network（§11.2）。
"""

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.sandbox.service import get_sandbox_service
from nnnu.services.sandbox.spec import ExecRequest

SNIPPET_MAX_CHARS = 2000


def _format_output(stdout: str, stderr: str) -> str:
    """stdout 为主，stderr 非空时附加（各自截断，防把模型上下文灌爆）。"""
    lines: list[str] = []
    if stdout.strip():
        lines.append(stdout.strip()[:SNIPPET_MAX_CHARS])
    if stderr.strip():
        lines.append(f"[stderr] {stderr.strip()[:SNIPPET_MAX_CHARS]}")
    return "\n".join(lines) if lines else "（无输出）"


class CodeExecutionTool(BaseTool):
    definition = ToolDefinition(
        name="code_execution",
        description="tools.code_execution",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                },
                "allow_network": {
                    "type": "boolean",
                    "default": False,
                    "description": "请求联网（默认关闭；需用户在设置里开启「沙箱允许联网」才生效）",
                },
            },
            "required": ["code"],
        },
        mount=ToolMount.ALWAYS,  # §6.3 context_gated 语义"有沙箱"：子进程沙箱恒在，等效恒挂载
        cost_hint="tools.cost.code_execution",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        request = ExecRequest(
            code=str(ctx.args.get("code", "")),
            timeout_s=float(ctx.args.get("timeout_seconds", 30)),
            allow_network=bool(ctx.args.get("allow_network", False)),
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
        )
        result = await get_sandbox_service().run(request)
        if result.error:
            output = f"执行失败：{result.error}"
            if result.stdout.strip():
                output += f"\n{result.stdout.strip()[:SNIPPET_MAX_CHARS]}"
            return ToolResult(ok=False, output=output)
        output = _format_output(result.stdout, result.stderr)
        if result.truncated:
            output += "\n（输出超过上限已被截断）"
        return ToolResult(
            ok=True,
            output=output,
            detail={
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
            },
        )
