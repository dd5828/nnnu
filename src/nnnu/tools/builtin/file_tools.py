"""文件工具（§7.2 / §11.3）：workspace 内文件读写与目录列表。

路径安全：归一化（resolve）后必须落在 data/workspace/ 内，否则拒绝（fail-closed）。
"""

from pathlib import Path

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.runtime import home
from nnnu.services.sandbox.service import SandboxError

READ_MAX_BYTES = 512 * 1024
WRITE_MAX_BYTES = 1 * 1024 * 1024
LIST_MAX_ENTRIES = 200


def _workspace_root() -> Path:
    return home.get_data_root() / "user" / "workspace"


def _resolve_inside(path_str: str) -> Path:
    """相对路径解析并锁定 workspace 内；越界抛 SandboxError。"""
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / (path_str or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise SandboxError(f"路径越界：{path_str!r} 不允许访问 workspace 之外") from None
    return candidate


def _common_definition(
    name: str, description_key: str, properties: dict, required: list[str]
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description_key,
        parameters={"type": "object", "properties": properties, "required": required},
        mount=ToolMount.ALWAYS,  # §6.3：始终 → 文件工具
    )


class ListWorkspaceTool(BaseTool):
    definition = _common_definition(
        "list_workspace",
        "tools.list_workspace",
        {
            "path": {"type": "string", "description": "相对 workspace 的目录路径（默认根目录）"},
        },
        [],
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            directory = _resolve_inside(str(ctx.args.get("path", "") or ""))
        except SandboxError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not directory.is_dir():
            return ToolResult(ok=False, output=f"目录不存在：{ctx.args.get('path', '.')}")
        entries: list[str] = []
        for item in sorted(directory.iterdir()):
            mark = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{mark}")
            if len(entries) >= LIST_MAX_ENTRIES:
                entries.append("…（已截断）")
                break
        relative = directory.relative_to(_workspace_root())
        header = (
            f"workspace/{relative} 共 {len(entries)} 项："
            if str(relative) != "."
            else "workspace 根目录："
        )
        return ToolResult(
            ok=True, output="\n".join([header, *entries]) if entries else f"{header}（空目录）"
        )


class ReadWorkspaceFileTool(BaseTool):
    definition = _common_definition(
        "read_workspace_file",
        "tools.read_workspace_file",
        {"path": {"type": "string", "description": "相对 workspace 的文件路径"}},
        ["path"],
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            file_path = _resolve_inside(str(ctx.args.get("path", "")))
        except SandboxError as exc:
            return ToolResult(ok=False, output=str(exc))
        if not file_path.is_file():
            return ToolResult(ok=False, output=f"文件不存在：{ctx.args.get('path')}")
        data = file_path.read_bytes()
        if len(data) > READ_MAX_BYTES:
            return ToolResult(
                ok=False, output=f"文件过大（{len(data)} 字节，上限 {READ_MAX_BYTES}）"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(ok=False, output="二进制文件不支持读取（仅 UTF-8 文本）")
        return ToolResult(ok=True, output=text)


class WriteWorkspaceFileTool(BaseTool):
    definition = _common_definition(
        "write_workspace_file",
        "tools.write_workspace_file",
        {
            "path": {"type": "string", "description": "相对 workspace 的文件路径"},
            "content": {"type": "string", "description": "文件内容（UTF-8 文本）"},
        },
        ["path", "content"],
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            file_path = _resolve_inside(str(ctx.args.get("path", "")))
        except SandboxError as exc:
            return ToolResult(ok=False, output=str(exc))
        content = str(ctx.args.get("content", ""))
        if len(content.encode("utf-8")) > WRITE_MAX_BYTES:
            return ToolResult(
                ok=False,
                output=f"内容过大（{len(content.encode('utf-8'))} 字节，上限 {WRITE_MAX_BYTES}）",
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return ToolResult(
            ok=True,
            output=f"已写入 workspace/{file_path.relative_to(_workspace_root())}（{len(content)} 字符）",
        )
