"""web_fetch 工具（§7.2）：抓取网页转 Markdown。

安全（§11）：SSRF 防护——解析域名后拒绝私有/环回/链路本地地址；
超时 10s；内容上限 200KB。转换用 markitdown（与附件解析同一引擎）。
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

FETCH_TIMEOUT_S = 10.0
CONTENT_MAX_CHARS = 200_000


def _is_private_host(hostname: str) -> bool:
    """域名解析后任一地址落在私有段即拒绝（防 SSRF）。"""
    try:
        records = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            return True
    return False


def _validate_url(raw_url: str) -> str | None:
    """校验并归一化 URL；非法/内网地址返回错误文案。"""
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "URL 不合法（需要 http:// 或 https:// 开头）"
    if _is_private_host(parsed.hostname):
        return "该地址指向内网/本机，已拒绝访问"
    return None


class WebFetchTool(BaseTool):
    definition = ToolDefinition(
        name="web_fetch",
        description="tools.web_fetch",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网页地址"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 200000,
                    "default": 50000,
                    "description": "返回内容上限（字符数）",
                },
            },
            "required": ["url"],
        },
        mount=ToolMount.ALWAYS,  # §6.3：始终 → web_fetch
        cost_hint="抓取公网网页（超时 10s，内网地址拒绝）",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        url = str(ctx.args.get("url", "")).strip()
        error = _validate_url(url)
        if error:
            return ToolResult(ok=False, output=f"抓取失败：{error}")
        max_chars = int(ctx.args.get("max_chars", 50000))

        def fetch() -> tuple[str, str | None]:
            from markitdown import MarkItDown

            try:
                result = MarkItDown().convert(url)
                return result.text_content.strip(), None
            except Exception as exc:  # markitdown 抛混杂异常：统一转友好文案
                return "", f"{type(exc).__name__}: {exc}"

        try:
            text, fetch_error = await asyncio.wait_for(
                asyncio.to_thread(fetch), timeout=FETCH_TIMEOUT_S
            )
        except TimeoutError:
            return ToolResult(ok=False, output=f"抓取超时（{FETCH_TIMEOUT_S:g}s），请换一个地址")
        if fetch_error:
            return ToolResult(ok=False, output=f"抓取失败：{fetch_error}")
        if not text:
            return ToolResult(ok=False, output="页面没有可提取的文本内容")
        truncated = len(text) > max_chars
        text = text[:max_chars]
        if truncated:
            text += "\n…（内容过长已截断）"
        return ToolResult(ok=True, output=text, detail={"url": url})
