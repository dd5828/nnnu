"""笔记本工具（§7.15 正式版）：write_note / list_notebook，模型在回合里读写笔记本。

- **write_note**：把内容存成记录。目标本按 notebook 参数（名称或 id）找；没给就用
  默认本「速记」/「Notes」；给了名字但不存在也**自动建**——随手记是宽容语义，
  别让「本不存在」卡住一次记录（请求的目标像 id 却查无此本才算错）。
- **list_notebook**：三态——无参列笔记本（带记录数）；给 notebook 列该本记录
  （标题 + id）；给 record_id 返回单条全文。模型「先看再写」靠它。
- 工具层吞异常是惯例：失败也回 ok=False 的文本让模型自己圆场，不让工具炸掉回合。
"""

import logging
from typing import Any

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.notebooks.service import (
    NotebookError,
    NotebookService,
    get_notebook_service,
)

logger = logging.getLogger(__name__)

DEFAULT_NOTEBOOK_NAMES = {"zh": "速记", "en": "Notes"}
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
RECORD_FULL_CHARS = 4000  # 单条记录返回上限（与引用注入的单条预算同量级）


def _default_name(language: str) -> str:
    return DEFAULT_NOTEBOOK_NAMES.get(language, DEFAULT_NOTEBOOK_NAMES["zh"])


async def _resolve_target(
    service: NotebookService, requested: str, language: str
) -> tuple[str, str, bool]:
    """找到（或建出）目标本 → (notebook_id, notebook_name, created)。

    requested 为空用默认本；给了名称/id 先查，查不到的**名称**自动创建；
    形如 nb- 却查不到的按 id 处理（报不存在，不莫名建一个叫 nb-xxx 的本）。
    """
    if not requested:
        target_name = _default_name(language)
        for notebook in await service.list_notebooks():
            if notebook.name == target_name:
                return notebook.id, notebook.name, False
        created = await service.create_notebook(target_name)
        return created.id, created.name, True
    for notebook in await service.list_notebooks():
        if notebook.id == requested or notebook.name == requested:
            return notebook.id, notebook.name, False
    if requested.startswith("nb-"):
        raise NotebookError(f"笔记本 {requested} 不存在")
    created = await service.create_notebook(requested)  # 名称非法由服务层校验报错
    return created.id, created.name, True


class WriteNoteTool(BaseTool):
    definition = ToolDefinition(
        name="write_note",
        description="tools.write_note",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "记录正文（Markdown，数学式用 LaTeX 行内写法）",
                },
                "title": {"type": "string", "description": "记录标题（不给就取正文首个非空行）"},
                "notebook": {
                    "type": "string",
                    "description": "存到哪个笔记本（名称或 id；不给进默认「速记」本，名字不存在会自动建）",
                },
            },
            "required": ["content"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_notebook_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        try:
            notebook_id, notebook_name, created = await _resolve_target(
                service, str(ctx.args.get("notebook") or "").strip(), ctx.language
            )
            record = await service.add_record(
                notebook_id,
                record_type="note",
                content_md=str(ctx.args.get("content") or ""),
                title=str(ctx.args.get("title") or ""),
                source_ref=f"sess:{ctx.session_id}" if ctx.session_id else None,
            )
        except NotebookError as exc:
            return ToolResult(ok=False, output=f"写入笔记本失败：{exc}")
        except Exception as exc:  # 数据库层面的意外也要圆场，不让工具炸掉回合
            logger.exception("write_note 失败")
            return ToolResult(ok=False, output=f"写入笔记本失败：{exc}")
        suffix = "（顺带新建了笔记本）" if created else ""
        return ToolResult(
            ok=True,
            output=f"已写入笔记本「{notebook_name}」{suffix}：{record.title}（{record.id}）",
            detail={
                "notebook_id": notebook_id,
                "notebook_name": notebook_name,
                "record_id": record.id,
                "title": record.title,
                "notebook_created": created,
            },
        )


class ListNotebookTool(BaseTool):
    definition = ToolDefinition(
        name="list_notebook",
        description="tools.list_notebook",
        parameters={
            "type": "object",
            "properties": {
                "notebook": {
                    "type": "string",
                    "description": "看哪个笔记本的记录（名称或 id；不给只列笔记本）",
                },
                "record_id": {"type": "string", "description": "直接读某条记录的全文"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
            },
        },
        mount=ToolMount.USER_TOGGLEABLE,
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            service = get_notebook_service()
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))
        try:
            record_id = str(ctx.args.get("record_id") or "").strip()
            if record_id:
                return await self._read_record(service, record_id)
            requested = str(ctx.args.get("notebook") or "").strip()
            if requested:
                return await self._list_records(service, requested, _clamp(ctx.args.get("limit")))
            return await self._list_notebooks(service)
        except Exception as exc:
            logger.exception("list_notebook 失败")
            return ToolResult(ok=False, output=f"读取笔记本失败：{exc}")

    async def _read_record(self, service: NotebookService, record_id: str) -> ToolResult:
        record = await service.get_record(record_id)
        if record is None:
            return ToolResult(ok=False, output=f"记录 {record_id} 不存在。")
        notebook = await service.get_notebook(record.notebook_id)
        text = record.content_md
        cut = len(text) > RECORD_FULL_CHARS
        if cut:
            text = text[:RECORD_FULL_CHARS] + "…（已截断）"
        return ToolResult(
            ok=True,
            output=f"【{notebook.name if notebook else '?'} · {record.title}】\n{text}",
            detail={"record_id": record.id, "notebook_id": record.notebook_id},
        )

    async def _list_records(
        self, service: NotebookService, requested: str, limit: int
    ) -> ToolResult:
        target = None
        for notebook in await service.list_notebooks():
            if notebook.id == requested or notebook.name == requested:
                target = notebook
                break
        if target is None:
            return ToolResult(ok=False, output=f"笔记本「{requested}」不存在。")
        records = await service.list_records(target.id)
        if not records:
            return ToolResult(
                ok=True,
                output=f"笔记本「{target.name}」还没有记录。",
                detail={"notebook_id": target.id, "records": []},
            )
        lines = [
            f"笔记本「{target.name}」共 {len(records)} 条，列出最新的 {min(limit, len(records))} 条："
        ]
        for record in records[:limit]:
            lines.append(f"- {record.title}（{record.id}）")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            detail={
                "notebook_id": target.id,
                "records": [
                    {"record_id": record.id, "title": record.title, "created_at": record.created_at}
                    for record in records[:limit]
                ],
            },
        )

    async def _list_notebooks(self, service: NotebookService) -> ToolResult:
        notebooks = await service.list_notebooks()
        if not notebooks:
            return ToolResult(ok=True, output="还没有任何笔记本。", detail={"notebooks": []})
        lines = ["笔记本列表："]
        items: list[dict[str, Any]] = []
        for notebook in notebooks:
            count = len(await service.list_records(notebook.id))
            lines.append(f"- {notebook.name}（{count} 条，{notebook.id}）")
            items.append({"notebook_id": notebook.id, "name": notebook.name, "record_count": count})
        return ToolResult(ok=True, output="\n".join(lines), detail={"notebooks": items})


def _clamp(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(number, MAX_LIMIT))
