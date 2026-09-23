"""brainstorm 工具（§7.2）：发散思维——广度优先列想法+理由（次级 LLM 调用）。"""

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.llm.errors import LLMConfigError
from nnnu.tools.builtin.sub_llm import render_tool_prompt, sub_llm_call

MAX_IDEAS = 10


class BrainstormTool(BaseTool):
    definition = ToolDefinition(
        name="brainstorm",
        description="tools.brainstorm",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "发散思考的主题"},
                "num_ideas": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["topic"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.brainstorm",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        topic = str(ctx.args.get("topic", "")).strip()
        if not topic:
            return ToolResult(ok=False, output="主题不能为空。")
        num_ideas = min(int(ctx.args.get("num_ideas", 5)), MAX_IDEAS)
        prompt = render_tool_prompt("brainstorm", ctx.language, topic=topic, num_ideas=num_ideas)
        try:
            text, usage = await sub_llm_call(ctx, prompt, temperature=0.9)
        except LLMConfigError as exc:
            return ToolResult(ok=False, output=f"模型未配置或不可用：{exc}")
        except Exception as exc:
            return ToolResult(ok=False, output=f"发散思考调用失败：{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, output=text, usage=usage)
