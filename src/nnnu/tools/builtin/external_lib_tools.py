"""外部库工具（§7.9，P14）：Obsidian / IMA / MarginNote 的对话入口。

三组都是 CONTEXT_GATED，门键就是工具名（§6.3），由
`capabilities/_shared.py` 按「有没有挂载/连好」统一开关：

- Obsidian：挂了至少一个 vault 才挂三件；
- IMA：client id / api key / 知识库 ID 都配好才挂四件；
- MarginNote：至少配对过一台设备才挂四件。

工具只做「用户明确要做的事」：写笔记、写回 IMA、追加笔记都会改动用户的
外部数据，描述里写明只在用户要求时调用。凭证永不出现在工具参数或返回值里。
"""

from __future__ import annotations

import json
from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult

OBSIDIAN_TOOL_NAMES = ("obsidian_list", "obsidian_read", "obsidian_write")
IMA_TOOL_NAMES = ("ima_browse", "ima_search", "ima_read", "ima_write_note")
MARGINNOTE_TOOL_NAMES = (
    "marginnote_search",
    "marginnote_list",
    "marginnote_read",
    "marginnote_links",
)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _err(message: str) -> ToolResult:
    return ToolResult(ok=False, output=message)


def _ok(payload: Any, *, detail: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(ok=True, output=_json(payload), detail=detail)


def _obsidian_vault(service: Any, ref: str):
    from nnnu.services.external_libs.obsidian import VaultError

    vaults = service.list_vaults()
    if not vaults:
        raise VaultError("还没有挂载 Obsidian 库（知识中心 → 外部库）")
    wanted = str(ref or "").strip()
    if wanted:
        for vault in vaults:
            if wanted in {vault.vault_id, vault.name}:
                return vault
        names = "、".join(f"{vault.name}（{vault.vault_id}）" for vault in vaults)
        raise VaultError(f"没有这个 Obsidian 库：{wanted}；已挂载：{names}")
    if len(vaults) == 1:
        return vaults[0]
    names = "、".join(f"{vault.name}（{vault.vault_id}）" for vault in vaults)
    raise VaultError(f"挂了多个 vault，请用 vault 参数指定一个：{names}")


class ObsidianListTool(BaseTool):
    definition = ToolDefinition(
        name="obsidian_list",
        description="tools.obsidian_list",
        parameters={
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "vault 名字或 id（只挂了一个时可省略）"},
                "folder": {"type": "string", "description": "只列这个子目录（留空 = 整个库）"},
                "limit": {"type": "integer", "description": "最多列多少篇（默认 200，上限 1000）"},
            },
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.obsidian import VaultError, get_obsidian_service

        try:
            service = get_obsidian_service()
            vault = _obsidian_vault(service, str(ctx.args.get("vault") or ""))
            args = ctx.args or {}
            result = await service.list_notes(
                vault.vault_id,
                folder=str(args.get("folder") or ""),
                limit=max(1, min(int(args.get("limit") or 200), 1000)),
            )
        except (RuntimeError, VaultError, ValueError) as exc:
            return _err(f"列 Obsidian 笔记失败：{exc}")
        notes = result["notes"]
        return _ok(
            {
                "vault": vault.name,
                "folder": result["folder"],
                "count": len(notes),
                "notes": notes,
            }
        )


class ObsidianReadTool(BaseTool):
    definition = ToolDefinition(
        name="obsidian_read",
        description="tools.obsidian_read",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "vault 里的笔记相对路径，如 学习/傅里叶.md",
                },
                "vault": {"type": "string", "description": "vault 名字或 id（只挂了一个时可省略）"},
            },
            "required": ["path"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.obsidian import VaultError, get_obsidian_service

        path = str((ctx.args or {}).get("path") or "").strip()
        if not path:
            return _err("path 不能为空")
        try:
            service = get_obsidian_service()
            vault = _obsidian_vault(service, str((ctx.args or {}).get("vault") or ""))
            note = await service.read_note(vault.vault_id, path)
        except (RuntimeError, VaultError, ValueError) as exc:
            return _err(f"读 Obsidian 笔记失败：{exc}")
        return _ok(
            {
                "vault": vault.name,
                "path": note["path"],
                "title": note["title"],
                "size": note["size"],
                "content": note["content"],
            }
        )


class ObsidianWriteTool(BaseTool):
    definition = ToolDefinition(
        name="obsidian_write",
        description="tools.obsidian_write",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "vault 里的笔记相对路径（没有 .md 会自动补上）",
                },
                "content": {"type": "string", "description": "要写入的 Markdown 正文"},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": "overwrite 覆盖全文（默认）；append 追加到末尾",
                },
                "vault": {"type": "string", "description": "vault 名字或 id（只挂了一个时可省略）"},
            },
            "required": ["path", "content"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.obsidian_write",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.obsidian import VaultError, get_obsidian_service

        args = ctx.args or {}
        path = str(args.get("path") or "").strip()
        if not path:
            return _err("path 不能为空")
        try:
            service = get_obsidian_service()
            vault = _obsidian_vault(service, str(args.get("vault") or ""))
            result = await service.write_note(
                vault.vault_id,
                path,
                str(args.get("content") or ""),
                mode=str(args.get("mode") or "overwrite"),
            )
        except (RuntimeError, VaultError, ValueError) as exc:
            return _err(f"写 Obsidian 笔记失败：{exc}")
        return _ok(
            {
                "vault": vault.name,
                "path": result["path"],
                "created": result["created"],
                "mode": result["mode"],
                "size": result["size"],
            },
            detail={"path": result["path"], "mode": result["mode"]},
        )


class ImaBrowseTool(BaseTool):
    definition = ToolDefinition(
        name="ima_browse",
        description="tools.ima_browse",
        parameters={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "description": "要看哪个文件夹（留空 = 知识库根）"},
                "cursor": {"type": "string", "description": "上一页返回的 next_cursor"},
                "limit": {"type": "integer", "description": "每页条数（默认 50，上限 50）"},
            },
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.ima import ImaConfigError, ImaError, get_ima_service

        args = ctx.args or {}
        try:
            result = await get_ima_service().browse(
                folder_id=str(args.get("folder_id") or ""),
                cursor=str(args.get("cursor") or ""),
                limit=max(1, min(int(args.get("limit") or 50), 50)),
            )
        except (RuntimeError, ImaConfigError, ImaError, ValueError) as exc:
            return _err(f"浏览 IMA 知识库失败：{exc}")
        return _ok(result)


class ImaSearchTool(BaseTool):
    definition = ToolDefinition(
        name="ima_search",
        description="tools.ima_search",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词"},
                "limit": {"type": "integer", "description": "最多几条（默认 10，上限 50）"},
            },
            "required": ["query"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.ima import ImaConfigError, ImaError, get_ima_service

        args = ctx.args or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return _err("query 不能为空")
        try:
            result = await get_ima_service().search(
                query, limit=max(1, min(int(args.get("limit") or 10), 50))
            )
        except (RuntimeError, ImaConfigError, ImaError, ValueError) as exc:
            return _err(f"检索 IMA 知识库失败：{exc}")
        return _ok(result)


class ImaReadTool(BaseTool):
    definition = ToolDefinition(
        name="ima_read",
        description="tools.ima_read",
        parameters={
            "type": "object",
            "properties": {
                "media_id": {
                    "type": "string",
                    "description": "ima_browse / ima_search 返回的 media_id",
                },
            },
            "required": ["media_id"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.ima import ImaConfigError, ImaError, get_ima_service

        media_id = str((ctx.args or {}).get("media_id") or "").strip()
        if not media_id:
            return _err("media_id 不能为空")
        try:
            result = await get_ima_service().read_item(media_id)
        except (RuntimeError, ImaConfigError, ImaError, ValueError) as exc:
            return _err(f"读取 IMA 素材失败：{exc}")
        return _ok(result)


class ImaWriteNoteTool(BaseTool):
    definition = ToolDefinition(
        name="ima_write_note",
        description="tools.ima_write_note",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Markdown 正文"},
                "note_id": {"type": "string", "description": "追加到这篇笔记（留空 = 新建笔记）"},
                "folder_id": {"type": "string", "description": "新建笔记放到哪个笔记本（可选）"},
            },
            "required": ["content"],
        },
        mount=ToolMount.CONTEXT_GATED,
        cost_hint="tools.cost.ima_write_note",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.ima import ImaConfigError, ImaError, get_ima_service

        args = ctx.args or {}
        content = str(args.get("content") or "").strip()
        if not content:
            return _err("content 不能为空")
        note_id = str(args.get("note_id") or "").strip()
        try:
            service = get_ima_service()
            if note_id:
                result = await service.append_note(note_id, content)
            else:
                result = await service.create_note(
                    content, folder_id=str(args.get("folder_id") or "")
                )
        except (RuntimeError, ImaConfigError, ImaError, ValueError) as exc:
            return _err(f"写回 IMA 失败：{exc}")
        return _ok(result, detail=result)


class MarginNoteSearchTool(BaseTool):
    definition = ToolDefinition(
        name="marginnote_search",
        description="tools.marginnote_search",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜的文字（标题/正文/摘录/文档名/标签）",
                },
                "object_type": {
                    "type": "string",
                    "enum": ["note", "excerpt", "card", "mindmap_node", "document", "comment"],
                    "description": "只搜某一类对象（可选）",
                },
                "limit": {"type": "integer", "description": "最多几条（默认 20）"},
            },
            "required": ["query"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.marginnote import get_marginnote_service

        args = ctx.args or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return _err("query 不能为空")
        try:
            rows = get_marginnote_service().search(
                query,
                object_type=str(args.get("object_type") or ""),
                limit=max(1, min(int(args.get("limit") or 20), 100)),
            )
        except (RuntimeError, ValueError) as exc:
            return _err(f"搜索 MarginNote 失败：{exc}")
        return _ok({"query": query, "count": len(rows), "results": rows})


class MarginNoteListTool(BaseTool):
    definition = ToolDefinition(
        name="marginnote_list",
        description="tools.marginnote_list",
        parameters={
            "type": "object",
            "properties": {
                "object_type": {
                    "type": "string",
                    "enum": ["note", "excerpt", "card", "mindmap_node", "document", "comment"],
                    "description": "只看某一类对象（可选）",
                },
                "document_id": {"type": "string", "description": "只看某本文档下的对象（可选）"},
                "limit": {"type": "integer", "description": "最多几条（默认 200）"},
            },
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.marginnote import get_marginnote_service

        args = ctx.args or {}
        try:
            rows = get_marginnote_service().list_objects(
                object_type=str(args.get("object_type") or ""),
                document_id=str(args.get("document_id") or ""),
                limit=max(1, min(int(args.get("limit") or 200), 1000)),
            )
        except (RuntimeError, ValueError) as exc:
            return _err(f"列 MarginNote 数据失败：{exc}")
        return _ok({"count": len(rows), "objects": rows})


class MarginNoteReadTool(BaseTool):
    definition = ToolDefinition(
        name="marginnote_read",
        description="tools.marginnote_read",
        parameters={
            "type": "object",
            "properties": {
                "object_id": {"type": "string", "description": "搜索/列表返回的 object_id"},
            },
            "required": ["object_id"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.marginnote import get_marginnote_service

        object_id = str((ctx.args or {}).get("object_id") or "").strip()
        if not object_id:
            return _err("object_id 不能为空")
        try:
            row = get_marginnote_service().get(object_id)
        except RuntimeError as exc:
            return _err(f"读取 MarginNote 数据失败：{exc}")
        if row is None:
            return _err(f"MarginNote 里找不到对象：{object_id}")
        return _ok(row)


class MarginNoteLinksTool(BaseTool):
    definition = ToolDefinition(
        name="marginnote_links",
        description="tools.marginnote_links",
        parameters={
            "type": "object",
            "properties": {
                "object_id": {"type": "string", "description": "要查关系的 object_id"},
            },
            "required": ["object_id"],
        },
        mount=ToolMount.CONTEXT_GATED,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        from nnnu.services.external_libs.marginnote import get_marginnote_service

        object_id = str((ctx.args or {}).get("object_id") or "").strip()
        if not object_id:
            return _err("object_id 不能为空")
        try:
            rows = get_marginnote_service().linked_objects(object_id)
        except (RuntimeError, ValueError) as exc:
            return _err(f"查 MarginNote 关系失败：{exc}")
        return _ok({"object_id": object_id, "count": len(rows), "links": rows})


ALL_EXTERNAL_LIB_TOOLS = (
    ObsidianListTool,
    ObsidianReadTool,
    ObsidianWriteTool,
    ImaBrowseTool,
    ImaSearchTool,
    ImaReadTool,
    ImaWriteNoteTool,
    MarginNoteSearchTool,
    MarginNoteListTool,
    MarginNoteReadTool,
    MarginNoteLinksTool,
)
