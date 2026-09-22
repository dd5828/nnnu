"""imagegen / videogen 工具（§7.2）：图像/视频生成。

图像走 OpenAI 兼容 images/generations（复用当前 LLM 端点与密钥，
模型取设置 image_model）；生成图落盘 workspace/generated/ 并以
detail.data_uri 供前端工具卡直显。视频 P3 仅友好报错（偏离记录）。
"""

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.media import service as media_service


class ImageGenTool(BaseTool):
    definition = ToolDefinition(
        name="imagegen",
        description="tools.imagegen",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "图像描述（越具体越好，可用中文）"},
                "size": {
                    "type": "string",
                    "enum": ["1024x1024", "1792x1024", "1024x1792"],
                    "default": "1024x1024",
                },
            },
            "required": ["prompt"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="图像生成（按图像模型计费，较贵）",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        prompt = str(ctx.args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult(ok=False, output="图像描述不能为空。")
        result = await media_service.generate_image(
            prompt, size=str(ctx.args.get("size", "1024x1024"))
        )
        if not result.ok:
            return ToolResult(ok=False, output=f"图像生成失败：{result.error}")
        detail: dict = {}
        if result.data_uri:
            detail["image_data_uri"] = result.data_uri
        if result.url:
            detail["image_url"] = result.url
        location = f"已保存到 workspace/{result.path}" if result.path else f"图片地址：{result.url}"
        return ToolResult(
            ok=True,
            output=f"已生成图片《{prompt[:40]}》。{location}",
            detail=detail,
        )


class VideoGenTool(BaseTool):
    definition = ToolDefinition(
        name="videogen",
        description="tools.videogen",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "视频描述"},
            },
            "required": ["prompt"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="视频生成（尚未支持，随后续阶段补齐）",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        result = await media_service.generate_video(str(ctx.args.get("prompt", "")))
        return ToolResult(ok=False, output=result.error or "视频生成失败")
