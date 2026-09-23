"""web_search 工具（§7.2）：联网搜索，返回带来源链接的引用列表。

用户开关挂载（§6.3 user_toggleable）；命中经 detail.sources 转 citation 事件
（与 attachment_search 同一通路）。未配置提供商时用免密钥默认 DuckDuckGo。
"""

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.search import service as search_service

SNIPPET_MAX_CHARS = 400


class WebSearchTool(BaseTool):
    definition = ToolDefinition(
        name="web_search",
        description="tools.web_search",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或问题"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.web_search",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        query = str(ctx.args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, output="搜索词不能为空。")
        response = await search_service.web_search(query, int(ctx.args.get("max_results", 5)))
        if response.error:
            return ToolResult(ok=False, output=f"搜索失败：{response.error}")
        if not response.hits:
            return ToolResult(ok=True, output=f"「{query}」没有搜索到结果。")

        lines = [f"「{query}」搜索到 {len(response.hits)} 条结果（{response.provider}）："]
        sources: list[dict] = []
        for index, hit in enumerate(response.hits, 1):
            lines.append(
                f"[{index}] {hit.title}\n    {hit.url}\n    {hit.snippet[:SNIPPET_MAX_CHARS]}"
            )
            sources.append(
                {
                    "doc_id": hit.url,
                    "kb": "web",
                    "page": None,
                    "snippet": hit.snippet[:SNIPPET_MAX_CHARS],
                }
            )
        return ToolResult(ok=True, output="\n".join(lines), detail={"sources": sources})
