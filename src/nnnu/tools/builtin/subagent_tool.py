"""子代理咨询工具（§7.12）：把活交给本机的编码 AI，把它说的话带回来。

CONTEXT_GATED，门键就是工具名——**至少配了一条连接**才挂（capabilities/_shared.py
算这个条件，fail-closed）。一条都没配时把它挂给模型，等于给一个只会报错的工具；
反过来，配了之后的开关权还在用户手上（设置里的工具禁用线照旧能关掉它）。

三件在别处容易漏掉的事：

- **模型指定不了权限档位**：跑起来是 `bypassPermissions`（无头模式没有交互确认），
  所以「哪条连接、什么权限」只能来自用户在界面上建的连接配置，模型只给任务正文；
- **输出要截**：子代理的结论会作为 tool 消息进下一轮上下文，原样带一份万字级的
  报告能把预算吃穿；
- **每次咨询落审计**（§11.6）：跑的是本机上别人家的程序，以本用户的权限读写文件。
"""

from __future__ import annotations

from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.agents import cli_sessions
from nnnu.services.agents.service import get_agent_service
from nnnu.subagents.runner import MAX_TIMEOUT_S, run_consult

#: 进上下文（tool 消息）的结论上限
OUTPUT_MAX_CHARS = 8000
#: 审计里留的任务片段长度
AUDIT_TASK_MAX_CHARS = 200


def _render(ctx: ToolContext, key: str, **values: Any) -> str:
    from nnnu.services.i18n.prompts import get_prompt_manager

    return get_prompt_manager().render("agents", ctx.language, key, **values)


def _clip(text: str, limit: int = OUTPUT_MAX_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class ConsultSubagentTool(BaseTool):
    definition = ToolDefinition(
        name="consult_subagent",
        description="tools.consult_subagent",
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "交给子代理的完整任务描述：目标、要在哪个目录/文件上做、期望产出。"
                        "它看不到本回合的对话，得把背景写进去。"
                    ),
                },
                "connection": {
                    "type": "string",
                    "description": "可选：用哪条子代理连接（不填就用本会话选中的那条）",
                },
                "timeout_s": {
                    "type": "number",
                    "description": f"可选：最长等待秒数（默认 600，最多 {int(MAX_TIMEOUT_S)}）",
                },
            },
            "required": ["task"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.consult_subagent",  # 双语键：chat.yaml tool_cost_hints
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        service = get_agent_service()
        task = str(ctx.args.get("task") or "").strip()
        if not task:
            return ToolResult(ok=False, output=_render(ctx, "empty_task"))

        wanted = str(ctx.args.get("connection") or "").strip()
        selected = str(ctx.metadata.get("agent_connection") or "")
        connection = service.resolve(wanted, session_selected=selected)
        if connection is None:
            rows = service.list()
            names = "、".join(f"{item.id}（{item.label()}）" for item in rows) or "（无）"
            key = (
                "unknown_connection" if wanted else ("pick_connection" if rows else "no_connection")
            )
            return ToolResult(
                ok=False,
                output=_render(ctx, key, connection=wanted, connections=names),
                detail={"connections": [item.id for item in rows]},
            )
        if not connection.supported:
            return ToolResult(
                ok=False,
                output=_render(
                    ctx, "unsupported_kind", connection=connection.label(), kind=connection.kind
                ),
                detail={"connection": connection.id, "kind": connection.kind},
            )

        timeout = ctx.args.get("timeout_s")
        emit = ctx.metadata.get("subagent_emit")
        resume = cli_sessions.lookup(ctx.session_id, connection.id)
        result = await run_consult(
            connection,
            task,
            resume=resume,
            timeout_s=float(timeout) if isinstance(timeout, (int, float)) else 0.0,
            emit=emit if callable(emit) else None,
        )
        if result.stale_session and resume:
            # CLI 那边这段会话没了（过期/被清）：丢掉记录、重开一段，别让用户白等一次失败
            await cli_sessions.forget(ctx.session_id, connection.id)
            result = await run_consult(
                connection,
                task,
                timeout_s=float(timeout) if isinstance(timeout, (int, float)) else 0.0,
                emit=emit if callable(emit) else None,
            )
            resume = ""
        if result.session_id:
            await cli_sessions.remember(ctx.session_id, connection.id, result.session_id)

        from nnnu.services.audit import audit_log

        audit_log(
            "subagent_consult",
            connection=connection.id,
            kind=connection.kind,
            task=task[:AUDIT_TASK_MAX_CHARS],
            ok=result.ok,
            stop_reason=result.stop_reason,
            duration_s=round(result.duration_s, 1),
            resumed=bool(resume),
        )

        detail: dict[str, Any] = {
            "connection": connection.id,
            "connection_label": connection.label(),
            "agent_session_id": result.session_id,
            "stop_reason": result.stop_reason,
            "duration_s": round(result.duration_s, 1),
            "resumed": bool(resume),
        }
        output = self._compose(ctx, connection, result, resumed=bool(resume))
        return ToolResult(ok=result.ok, output=output, detail=detail)

    @staticmethod
    def _compose(ctx: ToolContext, connection, result, *, resumed: bool) -> str:
        """结论正文：抬头 + 正文（截断）+ 没跑完/失败的原因。

        失败也把正文带上：被杀掉的那一趟往往已经说了半截结论，主模型看得见总比
        只收到一句「失败」有用（真没话说才只剩原因）。
        """
        header = _render(
            ctx, "header_resumed" if resumed else "header", connection=connection.label()
        )
        body = result.output
        if not body.strip():
            extra = result.error or "、".join(result.notes)
            return _render(ctx, "no_output", extra=f"（{extra}）" if extra else "")
        text, clipped = _clip(body)
        parts = [header, text]
        if clipped:
            parts.append(_render(ctx, "truncated", limit=str(OUTPUT_MAX_CHARS)))
        if not result.ok:
            if result.error:
                parts.append(
                    _render(ctx, "failed", connection=connection.label(), error=result.error)
                )
            else:
                parts.append(_render(ctx, "incomplete"))
        parts.extend(f"（{note}）" for note in result.notes)
        return "\n\n".join(part for part in parts if part)
