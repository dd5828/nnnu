"""MCP 工具的线上名与适配器（§7.17）。

两件事：

* **名字**。MCP 工具名是服务器说了算的（`read_file`、`search/issues`，什么都可能），
  而本仓工具名要同时过三关：OpenAI function calling 的 `[a-zA-Z0-9_-]{1,64}`、
  日志/前端的一眼可读、以及**不撞车**。于是线上名统一 `mcp_<server>_<tool>`
  （非法字符换下划线），超 64 字符按 `mcp_<前16>_<前32>_<sha1[:8]>` 收口——
  直接砍尾巴会让两台服务器上同前缀的工具撞成同名，哈希才是那个唯一化的东西。
  展示名 `mcp:<server>:<tool>` 留在 `detail` 里给界面用（冒号非法，只能展示）。

* **适配器**。把本仓的 `ToolContext` 收成一次 MCP 调用，再把结果翻成模型能读的话。
  服务器断连、等授权、超时这些都不是「空结果」，得说清楚发生了什么并带上原始错误
  （已在 manager 侧打过码），否则模型会以为工具只是干完了没输出。
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.i18n.prompts import get_prompt_manager

if TYPE_CHECKING:
    from mcp.client.session import ProgressFnT
    from nnnu.services.mcp.manager import MCPConnectionManager

logger = logging.getLogger(__name__)

#: OpenAI function name 的字符集（`[a-zA-Z0-9_-]`，最长 64）。
NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")
MAX_TOOL_NAME_CHARS = 64

#: 单次工具调用回给模型的上限：MCP 服务器可能吐回整份文件，塞进上下文就再也出不去。
OUTPUT_MAX_CHARS = 20000


def wrapped_name(server_id: str, tool_name: str) -> str:
    """`mcp_<server>_<tool>`（sanitize；超长按哈希收口，见模块头）。"""
    server = NAME_SANITIZE_RE.sub("_", server_id)
    tool = NAME_SANITIZE_RE.sub("_", tool_name)
    name = f"mcp_{server}_{tool}"
    if len(name) <= MAX_TOOL_NAME_CHARS:
        return name
    digest = hashlib.sha1(f"{server_id}/{tool_name}".encode("utf-8")).hexdigest()[:8]
    # 4 + 16 + 1 + 32 + 1 + 8 = 62，留 2 字符余量给将来调整
    return f"mcp_{server[:16]}_{tool[:32]}_{digest}"


def display_name(server_id: str, tool_name: str) -> str:
    """界面/日志里的那个名字（§7.17 的命名空间写法）。"""
    return f"mcp:{server_id}:{tool_name}"


ProgressFn = Callable[[str], Awaitable[None]]


def progress_reporter(progress_fn: ProgressFn, server_id: str) -> "ProgressFnT":
    """MCP 的进度通知 → 一行文本 → `progress_fn`（贴回工具卡）。

    服务器爱发什么发什么：有 message、只有数字、或者干脆一个空 tick。三种都得能渲染
    成东西——一个老老实实报进度的服务器，和一个什么都不报的，不能长得一样。

    本函数**绝不抛**：它跑在 SDK 的通知处理器里，这里炸掉会表现为「工具调用坏了」，
    而不是「少了一行状态」。
    """

    async def _report(progress: float, total: float | None, message: str | None) -> None:
        text = (message or "").strip()
        if total and total > 0:
            percent = int(round(max(0.0, min(1.0, progress / total)) * 100))
            text = f"{text} ({percent}%)" if text else f"{percent}%"
        elif not text:
            text = f"step {progress:g}"
        try:
            await progress_fn(text)
        except Exception:
            logger.debug("MCP 进度发布失败：%s", server_id, exc_info=True)

    return _report


class MCPToolAdapter(BaseTool):
    """一台 MCP 服务器的一个工具，包成本仓的 BaseTool。"""

    def __init__(
        self,
        *,
        manager: "MCPConnectionManager",
        server_id: str,
        server_label: str,
        tool_name: str,
        description: str,
        parameters: dict[str, Any] | None,
        timeout_s: int,
    ) -> None:
        self._manager = manager
        self.server_id = server_id
        self.server_label = server_label
        self.tool_name = tool_name  # 服务器认的名字，调用时用它（线上名是包装后的）
        self._timeout_s = timeout_s
        self.definition = ToolDefinition(
            name=wrapped_name(server_id, tool_name),
            # 描述前缀带服务器名：模型在一排工具里要分得清哪个来自哪家
            description=f"[{server_label}] {description or tool_name}",
            parameters=parameters or {"type": "object", "properties": {}},
            # 外部工具默认可关：用户在 MCP 页逐个停用，走的是挂载开关那条路（§6.3）
            mount=ToolMount.USER_TOGGLEABLE,
        )

    async def run(self, ctx: ToolContext) -> ToolResult:
        progress = ctx.metadata.get("progress_fn")
        on_progress = progress_reporter(progress, self.server_id) if callable(progress) else None
        outcome = await self._manager.call_tool(
            self.server_id,
            self.tool_name,
            ctx.args,
            timeout_s=self._timeout_s,
            on_progress=on_progress,
        )
        detail: dict[str, Any] = {
            "mcp_server": self.server_id,
            "mcp_server_name": self.server_label,
            "mcp_tool": self.tool_name,
            "mcp_display": display_name(self.server_id, self.tool_name),
        }
        if not outcome.ok:
            return ToolResult(
                ok=False, output=self._failure_text(outcome, ctx.language), detail=detail
            )
        text = outcome.text
        if len(text) > OUTPUT_MAX_CHARS:
            text = text[:OUTPUT_MAX_CHARS] + _render(
                ctx.language,
                "truncated",
                {
                    "zh": "\n\n（输出过长，已截断到 {limit} 字）",
                    "en": "\n\n(output truncated to {limit} characters)",
                },
                limit=OUTPUT_MAX_CHARS,
            )
        return ToolResult(ok=True, output=text, detail=detail)

    def _failure_text(self, outcome: Any, language: str) -> str:
        """把失败翻译成模型能照着行动的一句话（带上原始错误）。"""
        server = self.server_label
        if outcome.reason == "needs_auth":
            return _render(
                language,
                "needs_auth",
                {
                    "zh": "（MCP 服务器《{server}》还没授权：请用户在「技能 → MCP」里完成授权后重试）",
                    "en": '(MCP server "{server}" is not authorized yet; ask the user to finish '
                    "authorization on the Skills → MCP page, then retry)",
                },
                server=server,
            )
        if outcome.reason == "not_connected":
            return _render(
                language,
                "connect_failed",
                {
                    "zh": "（MCP 服务器《{server}》当前没连上：{error}）",
                    "en": '(MCP server "{server}" is not connected: {error})',
                },
                server=server,
                error=outcome.error or "—",
            )
        if outcome.reason == "timeout":
            return _render(
                language,
                "connect_timeout",
                {
                    "zh": "（MCP 服务器《{server}》上的调用超过 {seconds} 秒没有返回，已放弃）",
                    "en": '(the call on MCP server "{server}" did not return within {seconds}s and '
                    "was abandoned)",
                },
                server=server,
                seconds=str(self._timeout_s),
            )
        return _render(
            language,
            "tool_failed",
            {
                "zh": "（MCP 工具 {tool} 调用失败：{error}）",
                "en": "(MCP tool {tool} failed: {error})",
            },
            tool=display_name(self.server_id, self.tool_name),
            error=outcome.error or outcome.text or "unknown error",
        )


def _render(language: str, key: str, fallback: dict[str, str], **vars: Any) -> str:
    """mcp.yaml 里的文案；键缺失（提示词目录没铺到）就用内置兜底。

    兜底也要分语言：提示词文件缺失时给英文模型塞一句中文报错，比报错本身更糟。
    """
    text = get_prompt_manager().render("mcp", language, key, **vars)
    if text:
        return text
    try:
        return fallback.get(language, fallback["en"]).format(**vars)
    except (KeyError, IndexError, ValueError):  # pragma: no cover - 兜底模板写错才到
        return fallback["en"]


__all__ = [
    "MAX_TOOL_NAME_CHARS",
    "OUTPUT_MAX_CHARS",
    "MCPToolAdapter",
    "display_name",
    "wrapped_name",
]
