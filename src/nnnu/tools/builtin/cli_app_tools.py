"""命令行应用工具（§7.18）：list_cli_apps 看有什么、run_cli_app 真的跑起来。

两个都是 CONTEXT_GATED，门键就是各自的名字（§6.3：context 集合存工具名）——
**装了至少一件应用**才挂（capabilities/_shared.py 算这个条件，fail-closed）。
没装任何东西时把 run_cli_app 挂给模型，等于给它一个只会报错的工具。

清单里带每个应用的**参数模板**（必填项、可选项、怎么填）：模型要凭它拼 args，
而它是从目录里现读的——这比让模型凭记忆猜 `ffmpeg -i` 还是 `ffmpeg --input` 靠谱。
"""

from __future__ import annotations

from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

LIST_MAX_ROWS = 20  # 一次列太多行既占上下文又不好挑


def _render(ctx: ToolContext, key: str, **values: Any) -> str:
    from nnnu.services.i18n.prompts import get_prompt_manager

    return get_prompt_manager().render("cli_apps", ctx.language, key, **values)


def _param_lines(entry: Any, ctx: ToolContext) -> list[str]:
    """参数模板 → 每个参数一行（名字、必填与否、说明）。"""
    schema = entry.params_schema or {}
    props = schema.get("properties") or {}
    required = set(entry.required_params)
    lines: list[str] = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append(
            _render(
                ctx,
                "param_line",
                name=name,
                note=_render(ctx, "param_required" if name in required else "param_optional"),
                description=str(spec.get("description") or ""),
            )
        )
    return lines


class ListCliAppsTool(BaseTool):
    definition = ToolDefinition(
        name="list_cli_apps",
        description="tools.list_cli_apps",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "按名字或用途搜索目录（留空 = 只列已装的应用）",
                }
            },
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.list_cli_apps",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.cli_apps.service import get_cli_app_service

        query = str(ctx.args.get("query") or "").strip()
        service = get_cli_app_service()
        # 带查询词 = 找「目录里还有什么」，不带 = 列「手头有什么」，两种问题两种答案
        rows = service.list(query, installed_only=not query)
        if not rows:
            key = "search_empty" if query else "no_apps"
            return ToolResult(
                ok=True,
                output=_render(ctx, key, query=query),
                detail={"apps": [], "query": query},
            )
        rows = rows[:LIST_MAX_ROWS]
        header = (
            _render(ctx, "search_header", query=query)
            if query
            else _render(ctx, "installed_header")
        )
        parts = [header]
        for row in rows:
            status = "installed_badge" if row["installed"] else "available_badge"
            parts.append(
                _render(
                    ctx,
                    "app_line",
                    name=row["name"],
                    app=row["id"],
                    description=(row.get("description") or {}).get(ctx.language)
                    or (row.get("description") or {}).get("en", ""),
                    status=_render(ctx, status),
                )
            )
            entry = service.entry(row["id"], require=False)
            if entry is None:
                continue
            params = _param_lines(entry, ctx)
            if params:
                parts.append(_render(ctx, "params_header"))
                parts.extend("  " + line for line in params)
        parts.append(_render(ctx, "list_hint"))
        return ToolResult(
            ok=True,
            output="\n".join(parts),
            detail={"apps": [row["id"] for row in rows], "query": query},
        )


class RunCliAppTool(BaseTool):
    definition = ToolDefinition(
        name="run_cli_app",
        description="tools.run_cli_app",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "应用 id（见 list_cli_apps 的输出）"},
                "args": {
                    "type": "object",
                    "description": "按该应用的参数模板填（必填项不能少；多余的可放 extra 数组）",
                    "additionalProperties": True,
                },
                "timeout_s": {"type": "number", "description": "可选：本次执行的超时秒数"},
            },
            "required": ["app"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.run_cli_app",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.cli_apps.runner import run_app
        from nnnu.services.cli_apps.service import get_cli_app_service

        app_id = str(ctx.args.get("app") or "").strip()
        service = get_cli_app_service()
        entry = service.entry(app_id, require=False)
        if entry is None:
            installed = service.list(installed_only=True)
            return ToolResult(
                ok=False,
                output=_render(
                    ctx,
                    "app_not_found",
                    app=app_id,
                    apps="、".join(row["id"] for row in installed[:LIST_MAX_ROWS]) or "（无）",
                ),
            )
        raw_args = ctx.args.get("args")
        if not isinstance(raw_args, dict):
            raw_args = {}
        timeout = ctx.args.get("timeout_s")
        result = await run_app(
            entry,
            raw_args,
            language=ctx.language,
            timeout_s=float(timeout) if isinstance(timeout, (int, float)) else None,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
        )
        return ToolResult(
            ok=result.ok,
            output=result.output,
            detail=result.as_detail(entry),
        )
