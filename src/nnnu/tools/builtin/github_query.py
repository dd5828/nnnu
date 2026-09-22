"""github 工具（§7.2）：查询仓库（README/文件树/指定文件内容）。

GitHub REST API（httpx）；token 可选（环境变量 GITHUB_TOKEN，公开仓库免 token，
限流更严）。内容上限 100KB，文件树截断 200 项。
"""

import httpx

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

GITHUB_API = "https://api.github.com"
TIMEOUT_S = 15.0
CONTENT_MAX_CHARS = 100_000
TREE_MAX_ENTRIES = 200


async def _github_get(path: str, *, raw: bool = False, token: str | None) -> str | dict:
    """GET GitHub API；raw=True 时拿文件原文，否则 JSON 对象。失败抛 RuntimeError（带状态码）。"""
    headers = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        response = await client.get(f"{GITHUB_API}{path}", headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"GitHub API 返回 HTTP {response.status_code}")
    if raw:
        return response.text
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub API 响应格式异常")
    return payload


class GithubQueryTool(BaseTool):
    definition = ToolDefinition(
        name="github",
        description="tools.github",
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "仓库名（owner/name 格式）"},
                "what": {
                    "type": "string",
                    "enum": ["readme", "tree", "file"],
                    "description": "查什么：readme 读 README；tree 列文件树；file 读指定文件",
                },
                "path": {
                    "type": "string",
                    "description": "文件路径（what=file 时必填，如 src/main.py）",
                },
                "branch": {"type": "string", "description": "分支名（默认仓库默认分支）"},
            },
            "required": ["repo", "what"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="GitHub API 查询（公开仓库免 token）",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        import os

        repo = str(ctx.args.get("repo", "")).strip().strip("/")
        what = str(ctx.args.get("what", ""))
        path = str(ctx.args.get("path", "")).strip().lstrip("/")
        branch = str(ctx.args.get("branch", "")).strip()
        token = os.environ.get("GITHUB_TOKEN")

        if "/" not in repo or len(repo.split("/")) != 2:
            return ToolResult(ok=False, output="repo 需要 owner/name 格式（如 HKUDS/DeepTutor）")
        try:
            if what == "readme":
                text = await _github_get(f"/repos/{repo}/readme", raw=True, token=token)
            elif what == "tree":
                suffix = f"?ref={branch}" if branch else ""
                payload = await _github_get(f"/repos/{repo}/git/trees/HEAD{suffix}", token=token)
                if not isinstance(payload, dict):
                    raise RuntimeError("GitHub API 响应格式异常")
                tree = payload.get("tree")
                if not isinstance(tree, list):
                    raise RuntimeError("仓库文件树为空")
                lines = [f"{repo} 文件树（{len(tree)} 项，最多显示 {TREE_MAX_ENTRIES}）："]
                for item in tree[:TREE_MAX_ENTRIES]:
                    marker = "/" if item.get("type") == "tree" else ""
                    lines.append(f"{item.get('path', '')}{marker}")
                return ToolResult(ok=True, output="\n".join(lines))
            elif what == "file":
                if not path:
                    return ToolResult(ok=False, output="what=file 需要 path 参数")
                suffix = f"?ref={branch}" if branch else ""
                text = await _github_get(
                    f"/repos/{repo}/contents/{path}{suffix}", raw=True, token=token
                )
            else:
                return ToolResult(ok=False, output=f"未知查询类型 {what}（readme/tree/file）")
        except RuntimeError as exc:
            return ToolResult(ok=False, output=f"GitHub 查询失败：{exc}")

        if not isinstance(text, str):
            return ToolResult(ok=False, output="GitHub API 响应格式异常")
        truncated = len(text) > CONTENT_MAX_CHARS
        output = text[:CONTENT_MAX_CHARS]
        if truncated:
            output += "\n…（内容过长已截断）"
        return ToolResult(ok=True, output=output)
