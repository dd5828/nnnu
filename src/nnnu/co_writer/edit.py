"""Co-Writer 改写管线（§7.13）：切片校验过的选区 → 提示词 → Agent 循环 → 清洗。

REST 面没有会话总线，这里的装配跟 chat 能力同构但全部是一次性的：
- 伪 UnifiedContext（session=co-writer-<doc_id>）只承载这次改写的语言/知识库/工具
  开关注入，**不落库**（sessions/messages/co_writer_docs 行数不变由 API 层用例守）；
- 裸 StreamBus 无人订阅也行：事件照常进 bus.history，工具轨迹拿它跟
  outcome.tool_calls 按 call_id 合并（前者带 args，后者带摘要/成败/detail）；
- 库名→kb_id 映射必须塞进 unified.metadata["rag_kbs"]（rag 工具的唯一取数口，
  见 tools/builtin/rag_tool.py）；
- 模型配置缺失/调用失败按 LLMError 抛出（路由映射 502/503，与题库 AI 同一条链）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from nnnu.capabilities._shared import mount_tools
from nnnu.co_writer.diff import diff_lines, diff_stats
from nnnu.co_writer.models import (
    clean_model_output,
    validate_action,
    validate_instruction,
)
from nnnu.core.agent_loop import LoopDeps, LoopOutcome, run_agent_loop
from nnnu.core.context import KbRef, SessionRef, UnifiedContext
from nnnu.core.events import StreamEventType
from nnnu.core.ids import new_id
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import ToolMountFlags
from nnnu.services.i18n.prompts import get_prompt_manager
from nnnu.services.llm.errors import LLMError
from nnnu.services.llm.factory import create_client, resolve_model_config
from nnnu.services.llm.reasoning import build_reasoning_kwargs
from nnnu.services.sessions.models import Message

EDIT_MAX_ROUNDS = 8  # 找回资料再动笔，八轮绰绰有余；防跑飞
EDIT_MAX_OUTPUT_TOKENS = 4096  # 与 chat 同档；整段太长走循环自带的截断续写
EDIT_TOKEN_BUDGET = 32000
ALLOWED_TOOLS = ("rag", "web_search", "web_fetch")


class CoWriterOutputError(LLMError):
    """模型这轮没给出可用的改写正文（整轮失败或清洗后为空）——路由按 502。"""


@dataclass(slots=True)
class EditOutcome:
    edited: str
    ops: list[dict[str, str]]
    stats: dict[str, int]
    trace: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    degraded: str | None  # 接地降级标记（如 kb_not_ready）；None = 一切正常
    model: str
    usage: dict[str, Any]  # cost.summary() 形状：{tokens, cost, per_model}


def context_text(prefix: str, suffix: str) -> str:
    """选区前后文拼成一段参考上下文（截断方向用省略号标出）。"""
    parts: list[str] = []
    if prefix.strip():
        parts.append("…" + prefix.strip())
    if suffix.strip():
        parts.append(suffix.strip() + "…")
    return "\n".join(parts)


def build_trace(outcome: LoopOutcome, bus: StreamBus) -> list[dict[str, Any]]:
    """工具轨迹：结果（摘要/成败/detail）按 call_id 配上调用参数。"""
    args_by_call: dict[str, dict[str, Any]] = {}
    for event in bus.history:
        if event.type == StreamEventType.TOOL_CALL:
            args_by_call[str(event.payload.get("call_id") or "")] = dict(
                event.payload.get("args") or {}
            )
    return [
        {**call, "args": args_by_call.get(str(call.get("call_id") or ""), {})}
        for call in outcome.tool_calls
    ]


async def run_edit(
    *,
    doc_id: str,
    selection: str,
    prefix: str,
    suffix: str,
    action: str,
    instruction: str,
    language: str,
    kb_ids: Sequence[str] = (),
    use_web: bool = False,
) -> EditOutcome:
    """跑一次改写。selection 已由调用方跟正文切片核对过（validate_selection）。"""
    action = validate_action(action)
    instruction = validate_instruction(action, instruction)
    lang = language if language in ("zh", "en") else "zh"
    prompts = get_prompt_manager()

    # 1) 伪上下文 + 工具挂载（勾了库才挂 rag；没开联网就按掉 web_search）
    unified = UnifiedContext(
        session=SessionRef(id=f"co-writer-{doc_id}"),
        capability="co_writer",
        message=Message.new(session_id=f"co-writer-{doc_id}", role="user", content=selection),
        kb_refs=[KbRef(kb_id=kb_id) for kb_id in kb_ids],
        tool_flags=ToolMountFlags(
            context={"rag"} if kb_ids else set(),
            suppressed=set() if use_web else {"web_search"},
        ),
        language=lang,
    )
    mounted = mount_tools(unified, allowlist=ALLOWED_TOOLS)
    unified.metadata["rag_kbs"] = mounted.rag_kbs
    degraded: str | None = None
    if kb_ids and not mounted.kb_names:
        degraded = "kb_not_ready"  # 勾了库但一个都没就绪：这轮没接地，如实降级

    # 2) 提示词拼装
    kb_note = (
        prompts.render("co_writer", lang, "kb_note", kbs=", ".join(mounted.kb_names))
        if mounted.kb_names
        else ""
    )
    system_prompt = prompts.render(
        "co_writer", lang, "system", tools=mounted.tool_lines(), kb_note=kb_note
    )
    blocks = [
        prompts.render(
            "co_writer",
            lang,
            "action_template",
            action_verb=prompts.render("co_writer", lang, f"action_verb.{action}"),
            instruction=instruction,
        )
    ]
    context = context_text(prefix, suffix)
    if context:
        blocks.append(prompts.render("co_writer", lang, "context_template", context=context))
    blocks.append(prompts.render("co_writer", lang, "user_template", text=selection))
    user_content = "\n\n".join(block for block in blocks if block)

    # 3) 模型与循环
    model_config = resolve_model_config()
    client = create_client(
        model_config.model,
        provider_id=model_config.provider_id,
        base_url=model_config.base_url,
        api_key=model_config.api_key,
    )
    deps = LoopDeps(
        client=client,
        tools=mounted.tool_set(),
        model=model_config.model,
        provider=model_config.provider_id or "",
        max_rounds=EDIT_MAX_ROUNDS,
        max_output_tokens=EDIT_MAX_OUTPUT_TOKENS,
        token_budget=EDIT_TOKEN_BUDGET,
        temperature=model_config.temperature,
        reasoning_effort=model_config.reasoning_effort,
        thinking_extra=build_reasoning_kwargs(
            provider_id=model_config.provider_id or "",
            model=model_config.model,
            reasoning_effort=model_config.reasoning_effort,
        ),
    )
    bus = StreamBus(turn_id=new_id("turn"))
    outcome = await run_agent_loop(
        unified,
        bus,
        deps,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )

    # 4) 清洗与结果
    if not outcome.completed:
        raise CoWriterOutputError("本轮改写没有跑完（模型调用出错），请重试")
    edited = clean_model_output(outcome.final_text, original=selection)
    if not edited:
        raise CoWriterOutputError("模型没有给出可用的改写结果，请重试")
    ops = diff_lines(selection, edited)
    return EditOutcome(
        edited=edited,
        ops=ops,
        stats=diff_stats(ops),
        trace=build_trace(outcome, bus),
        citations=outcome.citations,
        degraded=degraded,
        model=model_config.model,
        usage=unified.cost.summary(),
    )
