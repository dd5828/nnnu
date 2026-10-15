"""技能运行时工具（§7.16 / §6.3）：read_skill 读剧本、load_tools 查工具清单。

两个工具都是 CONTEXT_GATED，共用一道上下文条件：mount_tools 检测到有启用技能，
才把这两个工具名补进本回合的 flags.context（fail-closed，见 capabilities/_shared.py）。
read_skill 把 SKILL.md 的
frontmatter 摘要 + 正文交给模型，剧本点名的引用文件按需用 file 参数逐个读——
正文超 READ_SKILL_MAX_CHARS 截断并提示走引用文件。

输出末尾附本回合真实可用的工具名（ctx.metadata["mounted_tool_names"]）：技能剧本
常点名 exec/rag 这类按条件挂载的工具，点名的没挂时模型得如实告诉用户，别硬调。
"""

from __future__ import annotations

from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

READ_SKILL_MAX_CHARS = 12000  # SKILL.md 正文给模型的截断上限
READ_SKILL_FILE_MAX_CHARS = 12000  # 单个引用文件的截断上限


def _render(ctx: ToolContext, key: str, **values: Any) -> str:
    from nnnu.services.i18n.prompts import get_prompt_manager

    return get_prompt_manager().render("skills", ctx.language, key, **values)


def _list_text(names: list[str], ctx: ToolContext) -> str:
    return "、".join(names) if names else _render(ctx, "empty_list")


class ReadSkillTool(BaseTool):
    definition = ToolDefinition(
        name="read_skill",
        description="tools.read_skill",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名，必须是系统提示里列出的启用技能之一",
                },
                "file": {
                    "type": "string",
                    "description": "可选：读该技能的某个引用文件（见 SKILL.md 输出末尾的文件清单）",
                },
            },
            "required": ["name"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.skills.service import get_skill_service
        from nnnu.services.skills.spec import parse_skill_md

        try:
            service = get_skill_service()
        except RuntimeError:
            return ToolResult(ok=False, output=_render(ctx, "unavailable"))
        try:
            enabled = await service.enabled_names()
        except Exception:  # 库读不动就按「无启用技能」拒绝，别把回合拖挂
            enabled = []
        available = _list_text(enabled, ctx)
        name = str(ctx.args.get("name") or "").strip()
        if not name or name not in enabled:
            try:
                known = await service.exists(name) if name else False
            except Exception:
                known = False
            key = "disabled" if known else "not_found"
            return ToolResult(ok=False, output=_render(ctx, key, name=name, available=available))

        try:
            content = service.store.read(name)
            front, body = parse_skill_md(content)
            ref_files = service.store.list_ref_files(name)
        except (OSError, ValueError) as exc:  # 目录被手删 / 正文被手改坏：如实报错
            return ToolResult(ok=False, output=_render(ctx, "broken", name=name, error=str(exc)))

        file_rel = str(ctx.args.get("file") or "").strip()
        if file_rel:
            return self._read_file(ctx, service, name, file_rel, ref_files)

        lines = [_render(ctx, "skill_header", name=name, description=front.description)]
        if front.when_to_use:
            lines.append(_render(ctx, "when_to_use_line", when_to_use=front.when_to_use))
        lines.append("")
        truncated = len(body) > READ_SKILL_MAX_CHARS
        lines.append(body[:READ_SKILL_MAX_CHARS])
        if truncated:
            lines.append(_render(ctx, "truncated", limit=READ_SKILL_MAX_CHARS))
        if ref_files:
            lines.append(_render(ctx, "files_header", files=_list_text(ref_files, ctx)))
        tools = [str(item) for item in (ctx.metadata.get("mounted_tool_names") or [])]
        if tools:
            lines.append(_render(ctx, "tools_hint", tools="、".join(tools)))
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            detail={"skill": name, "files": ref_files, "truncated": truncated},
        )

    def _read_file(
        self, ctx: ToolContext, service: Any, name: str, file_rel: str, ref_files: list[str]
    ) -> ToolResult:
        try:
            text = service.store.read_ref_file(name, file_rel)
        except (OSError, ValueError):
            return ToolResult(
                ok=False,
                output=_render(
                    ctx,
                    "file_not_found",
                    name=name,
                    file=file_rel,
                    files=_list_text(ref_files, ctx),
                ),
            )
        truncated = len(text) > READ_SKILL_FILE_MAX_CHARS
        output = _render(ctx, "file_header", name=name, file=file_rel)
        output += "\n" + text[:READ_SKILL_FILE_MAX_CHARS]
        if truncated:
            output += _render(ctx, "file_truncated", limit=READ_SKILL_FILE_MAX_CHARS)
        return ToolResult(ok=True, output=output, detail={"skill": name, "file": file_rel})


class LoadToolsTool(BaseTool):
    definition = ToolDefinition(
        name="load_tools",
        description="tools.load_tools",
        parameters={"type": "object", "properties": {}},
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.runtime.registry.tool_registry import get_tool_registry
        from nnnu.services.i18n.prompts import get_prompt_manager

        prompts = get_prompt_manager()
        mounted = {str(item) for item in (ctx.metadata.get("mounted_tool_names") or [])}
        ready: list[str] = []
        others: list[str] = []
        for definition in sorted(get_tool_registry().definitions(), key=lambda item: item.name):
            description = (
                prompts.render("chat", ctx.language, f"tool_descriptions.{definition.name}")
                or definition.description
            )
            line = _render(ctx, "tool_line", name=definition.name, description=description)
            (ready if definition.name in mounted else others).append(line)
        parts = [_render(ctx, "tools_header"), *ready]
        if others:
            parts.append(_render(ctx, "tools_other_header"))
            parts.extend(others)
        return ToolResult(
            ok=True,
            output="\n".join(parts),
            detail={"mounted": sorted(mounted), "total": len(ready) + len(others)},
        )
