"""演示/验收工具（§14 验收②）：真实端点下跑一个回合，stdout 逐事件打印。

用法：
    DEEPSEEK_API_KEY=sk-xxx python -m nnnu.api.demo_turn "用一句话解释傅里叶变换"
    NNNU_LLM_MOCK=scripted NNNU_LLM_SCRIPT=scripts/demo.yaml python -m nnnu.api.demo_turn "离线演示"
"""

import asyncio
import sys

from nnnu.api import main as api_main  # noqa: F401  # 确保包可导入（app 工厂）
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.runtime import bootstrap
from nnnu.runtime.orchestrator import ChatOrchestrator
from nnnu.runtime.registry.capability_registry import get_capability_registry
from nnnu.runtime.registry.tool_registry import get_tool_registry
from nnnu.runtime.turn_runtime import TurnRequest, TurnRuntimeManager
from nnnu.services.cost.service import CostService
from nnnu.services.sessions.db import Database
from nnnu.services.sessions.schema import db_path, migrate
from nnnu.services.sessions.service import SessionManager


class DemoAddTool(BaseTool):
    """演示工具：整数加法（仅 demo_turn 进程内注册）。"""

    definition = ToolDefinition(
        name="add_numbers",
        description="整数加法",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        mount=ToolMount.ALWAYS,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output=str(ctx.args["a"] + ctx.args["b"]))


class DemoMultiplyTool(BaseTool):
    """演示工具：整数乘法（仅 demo_turn 进程内注册）。"""

    definition = ToolDefinition(
        name="multiply",
        description="整数乘法",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
        mount=ToolMount.ALWAYS,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output=str(ctx.args["x"] * ctx.args["y"]))


_EVENT_LABELS = {
    "turn_start": "▶ 回合开始",
    "status": "阶段",
    "thinking_delta": "思考",
    "tool_call": "工具调用",
    "tool_result": "工具结果",
    "content_delta": "回答",
    "content_done": "回答完成",
    "cost_summary": "成本",
    "done": "回合结束",
    "error": "错误",
    "stopped": "已停止",
}


async def run(question: str) -> int:
    from nnnu.runtime import home as runtime_home

    bootstrap.ensure_bootstrap()
    bootstrap.configure_logging()  # §12.4：回合日志落 data/user/logs（grep turn_id 可还原）
    migrate(runtime_home.get_data_root())
    db = Database(db_path(runtime_home.get_data_root()))
    await db.connect()
    try:
        sessions = SessionManager(db)
        costs = CostService(db)
        bootstrap.register_builtins()
        tools = get_tool_registry()
        tools.register(DemoAddTool())
        tools.register(DemoMultiplyTool())
        orchestrator = ChatOrchestrator(capabilities=get_capability_registry(), tools=tools)
        runtime = TurnRuntimeManager(sessions=sessions, costs=costs, orchestrator=orchestrator)
        started = await runtime.start_turn(TurnRequest(message=question))
        print(f"turn={started['turn_id']} session={started['session_id']}", flush=True)
        exit_code = 0
        async for event in runtime.subscribe_turn(started["turn_id"]):
            label = _EVENT_LABELS.get(event["type"], event["type"])
            payload = event["payload"]
            if event["type"] == "content_delta":
                print(payload.get("text", ""), end="", flush=True)
            elif event["type"] == "tool_call":
                print(f"\n[{label}] {payload.get('tool_name')} {payload.get('args')}", flush=True)
            elif event["type"] == "tool_result":
                print(
                    f"[{label}] ok={payload.get('ok')} {str(payload.get('summary'))[:120]}",
                    flush=True,
                )
            elif event["type"] == "cost_summary":
                print(
                    f"\n[{label}] tokens={payload.get('tokens')} cost=${payload.get('cost')}",
                    flush=True,
                )
            elif event["type"] == "error":
                print(f"\n[{label}] {payload.get('message')}", flush=True)
                exit_code = 1
        print("", flush=True)
        return exit_code
    finally:
        await db.close()


def main() -> None:
    # 重定向输出时固定 UTF-8（dev.py 同款）：Windows GBK 控制台会因 ↔ 等字符崩溃
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    question = " ".join(sys.argv[1:]) or "你好"
    sys.exit(asyncio.run(run(question)))


if __name__ == "__main__":
    main()
