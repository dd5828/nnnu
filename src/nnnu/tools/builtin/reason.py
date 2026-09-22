"""reason 工具（§7.2）：专门深度推理调用（次级 LLM，独立推理块）。"""

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.llm.errors import LLMConfigError
from nnnu.tools.builtin.sub_llm import render_tool_prompt, sub_llm_call


class ReasonTool(BaseTool):
    definition = ToolDefinition(
        name="reason",
        description="tools.reason",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "需要深度推理的问题"},
                "effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "high",
                    "description": "推理档位",
                },
            },
            "required": ["question"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="额外一次深度推理调用（较贵）",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        question = str(ctx.args.get("question", "")).strip()
        if not question:
            return ToolResult(ok=False, output="问题不能为空。")
        effort = str(ctx.args.get("effort", "high"))
        language = {"zh": "中文", "en": "English"}.get(ctx.language, ctx.language)
        prompt = render_tool_prompt("reason", ctx.language, question=question, language=language)
        try:
            text, usage = await sub_llm_call(ctx, prompt, reasoning_effort=effort, max_tokens=4096)
        except LLMConfigError as exc:
            return ToolResult(ok=False, output=f"模型未配置或不可用：{exc}")
        except Exception as exc:
            return ToolResult(ok=False, output=f"深度推理调用失败：{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, output=text, usage=usage)
