"""ask_user 内置工具（§7.2）：回合中途暂停提问。

暂停/恢复机制：ctx.metadata["ask_user_fn"](question, options, ask_id,
allow_free_text=..., context=...) 由传输层（TurnRuntime）注入——emit ask_user
事件、阻塞等待 WS 答复或 5 分钟超时空答复。无注入（非 WS 环境）时优雅失败，
不悬挂回合。

选项两种写法都收：{label, description} 对象（推荐，卡上显示短标签 + 长说明）
与纯字符串（归一成 {label: 原串, description: ""}）。
"""

import logging
from typing import Any

from nnnu.core.ids import new_id
from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

logger = logging.getLogger(__name__)


def normalize_options(raw: Any) -> list[dict[str, str]]:
    """把模型给的选项归一成 {label, description} 列表（纯字符串 / 缺字段都兜住）。"""
    options: list[dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            description = str(item.get("description", "") or "").strip()
        else:
            label = str(item).strip()
            description = ""
        if label:
            options.append({"label": label, "description": description})
    return options


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
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "选项短标签（用户点它作答）",
                            },
                            "description": {"type": "string", "description": "选项说明（可空）"},
                        },
                        "required": ["label"],
                    },
                    "description": "可选项列表（可为空；为空时卡片只收自由文本）",
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
        options = normalize_options(ctx.args.get("options"))
        ask_id = new_id("ask")
        answer = await ask_fn(question, options, ask_id, allow_free_text=True)
        return ToolResult(ok=True, output=answer or "(用户未作答)")
