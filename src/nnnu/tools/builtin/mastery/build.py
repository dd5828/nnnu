"""`mastery_build`：把一份知识点树建成学习路径（零 LLM，节点树由模型设计）。

**窄幂等**：只有「本会话正绑着的这条」主题也对得上才原样返回（挡模型重试）；
同一主题允许多条路径——用户想重学一遍就该能再建一条。
"""

from __future__ import annotations

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.learning.service import LearningError, get_learning_service
from nnnu.tools.builtin.mastery.common import MAX_NODES, status_lines, tree_lines

_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "节点标题（一句话能说清的知识点）"},
        "type": {
            "type": "string",
            "enum": ["memory", "procedure", "concept", "design"],
            "default": "concept",
            "description": (
                "memory 记忆 / procedure 操作 / concept 概念 / design 设计——"
                "前两类靠做题到 90 分过关，后两类靠用户自己讲一遍判定过关"
            ),
        },
        "description": {"type": "string", "description": "这个节点要学什么（可选）"},
        "parent_index": {
            "type": "integer",
            "description": "父节点在 nodes 数组里的下标（从 0 起，必须小于自己的下标）；顶层节点不填",
        },
        "parent_title": {
            "type": "string",
            "description": "父节点标题（必须在这个节点之前出现过）；顶层节点不填",
        },
    },
    "required": ["title"],
}


class MasteryBuildTool(BaseTool):
    definition = ToolDefinition(
        name="mastery_build",
        description="tools.mastery_build",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "学习主题"},
                "title": {"type": "string", "description": "路径标题（不给就用主题）"},
                "summary": {"type": "string", "description": "一句话说明这条路径"},
                "nodes": {
                    "type": "array",
                    "items": _NODE_SCHEMA,
                    "description": "节点数组，**顺序即学习顺序**，子节点紧跟父节点",
                },
            },
            "required": ["topic", "nodes"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.mastery_build",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_learning_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        topic = str(ctx.args.get("topic") or "").strip()
        if not topic:
            return ToolResult(ok=False, output="需要 topic（学习主题）。")
        nodes = ctx.args.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return ToolResult(ok=False, output="需要 nodes 数组（至少一个节点）。")
        if len(nodes) > MAX_NODES:
            return ToolResult(ok=False, output=f"一条路径最多 {MAX_NODES} 个节点，请先拆小一点。")
        rows = [row for row in nodes if isinstance(row, dict)]
        if len(rows) != len(nodes):
            return ToolResult(ok=False, output="nodes 里每一项都得是节点对象。")

        bound = await service.get_path_by_session(ctx.session_id)
        if bound is not None and bound.topic.strip().casefold() == topic.casefold():
            detail = await service.get_path(bound.id)
            text = status_lines(ctx, detail) if detail else ""
            return ToolResult(
                ok=True,
                output=(
                    f"这条路径已经建好、也正绑在这个会话上（《{bound.title}》 {bound.id}），"
                    f"不重复建：\n{text}"
                ),
                detail={"path_id": bound.id, "created": False},
            )
        try:
            path, built = await service.create_path(
                topic=topic,
                title=str(ctx.args.get("title") or "").strip() or None,
                summary=str(ctx.args.get("summary") or "").strip() or None,
                nodes=rows,
                session_id=ctx.session_id or None,
            )
        except LearningError as exc:
            return ToolResult(ok=False, output=f"建路径失败：{exc}")
        detail = await service.get_path(path.id)
        target = detail.next_target if detail else None
        return ToolResult(
            ok=True,
            output=(
                f"已建好学习路径《{path.title}》（id={path.id}，共 {len(built)} 个节点，"
                f"顺序即学习顺序）：\n{tree_lines(built)}\n"
                f"下一目标：{target.reason if target else '—'}"
            ),
            detail={
                "action": "build",
                "path_id": path.id,
                "created": True,
                "nodes": [node.model_dump() for node in built],
                "next": target.model_dump() if target else None,
            },
        )
