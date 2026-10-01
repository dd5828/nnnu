"""记忆工具（§7.10）：read_memory / write_memory——模型在回合里读写长期记忆。

挂载是 user_toggleable（默认挂载、用户可在设置里禁用）：§6.3 原写 context_gated
（"有记忆 → 挂载"），但记忆的第一条就是 write_memory 自己写出来的——按"有记忆才
挂"会死锁在零记忆状态，模型永远看不到它（与 cron 工具同一个坑）。

- **read_memory**：L3 四篇 + 当前分面的 L2 摘录，总长硬上限 2400 字（`full=true`
  放宽到 8000）。记忆为空返回「暂无记忆」而不是报错。
- **write_memory**：只往 L2 追加，引用本回合的 user_message 行——那一行在
  begin_turn 时已经落盘（`service.turn_ref`），引用天然有效；拿不到锚点就**不写**
  （宁缺毋滥，不猜位置）。banned 守卫拦住绝对化句式，归一化去重防重复写。
- 工具层吞异常是惯例：失败也回 ok=False 的文本让模型自己圆场，不让工具炸掉回合。
"""

import logging
import time

from nnnu.core.tool_protocol import BaseTool, ToolContext, ToolDefinition, ToolMount, ToolResult
from nnnu.services.memory import paths
from nnnu.services.memory.consolidator import guards
from nnnu.services.memory.models import MemoryDoc, MemoryEntry, text_digest
from nnnu.services.memory.service import get_memory_service
from nnnu.services.memory.state import doc_key

logger = logging.getLogger(__name__)

L2_TEXT_LIMIT = 400
DEFAULT_BUDGET = 2400  # ≈1.5k token：模型每次读记忆的成本上限
FULL_BUDGET = 8000
DOC_LIMITS = {"l3": (8, 1200), "l2": (6, 1200)}  # (条目数上限, 该文档字符预算)
FULL_DOC_LIMITS = (100, 4000)


def _render_doc(doc: MemoryDoc, *, full: bool) -> str:
    """一篇文档 → 摘录文本（前 N 条、超出预算即停）。"""
    limit, budget = FULL_DOC_LIMITS if full else DOC_LIMITS[doc.layer]
    lines: list[str] = []
    used = 0
    for entry in doc.entries[:limit]:
        mark = "（此条已被标为过期）" if entry.stale else ""
        line = f"- [{entry.date}] {entry.text}{mark}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _target_surface(service, ctx: ToolContext, requested: str) -> tuple[str | None, str | None]:
    """定写入/读取的分面：显式给了就校验，没给用本回合分面（拿不到退 chat）。"""
    if requested:
        return (requested, None) if requested in paths.SURFACES else (None, f"未知分面 {requested}")
    remembered = service.surface_for_turn(ctx.turn_id) or "chat"
    return remembered, None


class ReadMemoryTool(BaseTool):
    definition = ToolDefinition(
        name="read_memory",
        description="tools.read_memory",
        parameters={
            "type": "object",
            "properties": {
                "layer": {
                    "type": "string",
                    "enum": ["all", "l3", "l2"],
                    "description": "读哪层：all（默认）= L3 画像 + 当前分面 L2；l3 只读画像；l2 只读分面事实",
                },
                "surface": {
                    "type": "string",
                    "description": "L2 读哪个分面（默认当前分面，一般不用填）",
                },
                "full": {
                    "type": "boolean",
                    "description": "放宽长度上限（默认 2400 字，full 为 8000 字）",
                },
            },
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.read_memory",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        service = get_memory_service()
        if service is None:
            return ToolResult(ok=False, output="记忆未启用。")
        layer = str(ctx.args.get("layer") or "all")
        if layer not in ("all", "l3", "l2"):
            return ToolResult(ok=False, output=f"未知层 {layer}（all/l3/l2）。")
        full = bool(ctx.args.get("full"))
        surface, error = _target_surface(service, ctx, str(ctx.args.get("surface") or "").strip())
        if error or surface is None:
            return ToolResult(ok=False, output=error or "分面未知。")
        budget = FULL_BUDGET if full else DEFAULT_BUDGET
        try:
            sections: list[str] = []
            if layer in ("all", "l3"):
                for name in paths.L3_DOCS:
                    text = _render_doc(service.store.load("l3", name), full=full)
                    if text:
                        sections.append(f"【L3 · {paths.L3_TITLES.get(name, name)}】\n{text}")
            if layer in ("all", "l2"):
                text = _render_doc(service.store.load("l2", surface), full=full)
                if text:
                    sections.append(f"【L2 · {surface}】\n{text}")
        except Exception as exc:  # 工具层不炸回合
            logger.exception("read_memory 失败")
            return ToolResult(ok=False, output=f"读取记忆失败：{exc}")
        if not sections:
            return ToolResult(ok=True, output="暂无记忆。", detail={"empty": True})
        body = "\n\n".join(sections)
        cut = len(body) > budget
        if cut:
            body = body[:budget]
        header = "以下是关于用户的长期记忆（整合而来，供参考；与当前对话冲突时以当前为准）："
        tip = "\n（内容过长已截断，需要更全可以再读一次并指定 layer）" if cut else ""
        return ToolResult(
            ok=True,
            output=f"{header}\n{body}{tip}",
            detail={"layer": layer, "surface": surface, "chars": len(body), "truncated": cut},
        )


class WriteMemoryTool(BaseTool):
    definition = ToolDefinition(
        name="write_memory",
        description="tools.write_memory",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要记住的事实用一句话写完整（不超过 400 字），别把一次性状态写成长期结论",
                },
                "surface": {
                    "type": "string",
                    "description": "写进哪个分面（默认当前分面，一般不用填）",
                },
            },
            "required": ["text"],
        },
        mount=ToolMount.USER_TOGGLEABLE,
        cost_hint="tools.cost.write_memory",
    )

    async def run(self, ctx: ToolContext) -> ToolResult:
        service = get_memory_service()
        if service is None:
            return ToolResult(ok=False, output="记忆未启用。")
        text = str(ctx.args.get("text", "")).strip()
        if not text:
            return ToolResult(ok=False, output="text 不能为空。")
        anchor = service.turn_ref(ctx.turn_id)
        if anchor is None:
            return ToolResult(
                ok=False, output="记忆未启用或本回合没记录轨迹，拿不到引用锚点，这次没有写入。"
            )
        turn_surface, filename, line = anchor
        target, error = _target_surface(service, ctx, str(ctx.args.get("surface") or "").strip())
        if error or target is None:
            return ToolResult(ok=False, output=error or "分面未知。")
        banned = guards.find_banned(text)
        if banned is not None:
            return ToolResult(
                ok=False,
                output=f"「{banned}」是绝对化表述，长期记忆里别下这种定论——改成有条件的说法再写一次。",
            )
        if len(text) > L2_TEXT_LIMIT:
            text = guards.truncate(text, L2_TEXT_LIMIT)
        entry = MemoryEntry(
            id=None,
            date=time.strftime("%Y-%m-%d"),
            text=text,
            # 引用只认本回合那一行——分面可以另指，行永远指向真实落盘的锚点
            refs=[f"L1:{turn_surface}/{filename}#{line}"],
            origin="model",
            layer="l2",
            key=target,
        )
        try:
            written = await service.store.append_entries("l2", target, [entry])
            if not written:
                return ToolResult(
                    ok=True, output="这条记忆已经在了，没有重复写入。", detail={"duplicate": True}
                )
            stored = written[0]
            service.state.register_entry(
                doc_key("l2", target), stored.id or "", text_digest(stored.text), "model"
            )
        except Exception as exc:
            logger.exception("write_memory 失败")
            return ToolResult(ok=False, output=f"写入记忆失败：{exc}")
        return ToolResult(
            ok=True,
            output=f"已记到「{target}」分面（条目 {stored.id}）。",
            detail={"entry_id": stored.id, "surface": target, "ref": stored.refs[0]},
        )
