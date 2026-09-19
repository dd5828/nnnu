"""ask_user 内置工具（§7.2）：回合中途暂停提问。

暂停/恢复机制：ctx.metadata["ask_user_fn"](question, options, ask_id) 由传输层
（TurnRuntime）注入——emit ask_user 事件、阻塞等待 WS 答复或 5 分钟超时空答复。
无注入（非 WS 环境）时优雅失败，不悬挂回合。
"""

import logging

from nnnu.core.ids import new_id
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

logger = logging.getLogger(__name__)


class AskUserTool(BaseTool):
    definition = ToolDefinition(
        name="ask_user",
        description="tools.ask_user",  # 双语键：chat.yaml tool_descriptions
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选项列表（可为空）",
                },
            },
            "required": ["question"],
        },
        mount=ToolMount.ALWAYS,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        ask_fn = ctx.metadata.get("ask_user_fn")
        if ask_fn is None:
            return ToolResult(ok=False, output="当前环境不支持回合中途提问（无前端连接）")
        question = str(ctx.args.get("question", ""))
        options = [str(item) for item in (ctx.args.get("options") or [])]
        ask_id = new_id("ask")
        answer = await ask_fn(question, options, ask_id)
        return ToolResult(ok=True, output=answer or "(用户未作答)")
