"""rag 工具（§7.9）：挂载门、跨库检索合并、citation 对齐。

工具是「知识库 → 聊天」的唯一出口，这里盯三件事：
没有 ready 库时绝不挂载（fail-closed）；检索跨全部 ready 库；
detail.sources 的字段与 CitationSource 完全对得上（前端引用面板要靠它跳转）。
"""

import asyncio
from pathlib import Path

import pymupdf
import pytest
from conftest import TopicEmbedder

from nnnu.core.agent_loop import LoopDeps, ToolSet, run_agent_loop
from nnnu.core.context import SessionRef, UnifiedContext
from nnnu.core.events import CitationSource, DonePayload
from nnnu.core.stream_bus import StreamBus
from nnnu.core.tool_protocol import ToolContext, ToolMount, compute_mounted_tools
from nnnu.runtime.orchestrator import build_unified_context
from nnnu.runtime.turn_runtime import TurnRequest
from nnnu.services.embedding import service as embedding_service
from nnnu.services.embedding.service import EmbeddingService
from nnnu.services.knowledge import service as kb_module
from nnnu.services.knowledge.service import KBService
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.sessions.models import Message, Session
from nnnu.tools.builtin.rag_tool import RagSearchTool

SIGNAL_TEXT = "傅里叶变换把时域信号分解为频域分量之和。滤波与频谱分析都建立在这个变换上。"
BIO_TEXT = "光合作用把光能转化为化学能。叶绿体里的色素吸收光子，植物因此得以生长。"


@pytest.fixture(autouse=True)
def _isolate():
    yield
    embedding_service.uninstall_embedding_stub()
    kb_module.reset_kb_service()


def _pdf(path: Path, pages: int, text: str) -> bytes:
    doc = pymupdf.open()
    for index in range(1, pages + 1):
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(60, 60, 540, 700),
            f"第{index}页 {text}" * 3,
            fontname="china-s",
            fontsize=9,
        )
    doc.save(str(path))
    return path.read_bytes()


def _service(data_root: Path) -> KBService:
    embedding_service.install_embedding_stub(lambda: TopicEmbedder())
    service = KBService(data_root, embedder=EmbeddingService(batch_size=1))
    kb_module.set_kb_service(service)
    return service


async def _kb_with_doc(service: KBService, tmp_path: Path, name: str, text: str, pages: int = 1):
    kb = await service.create_kb(name)
    content = _pdf(tmp_path / f"{name}.pdf", pages, text)
    doc = await service.add_doc(kb.id, f"{name}.pdf", content, "application/pdf")
    await service.wait_idle(kb.id)
    return kb, doc


def _ctx(query: str, **args) -> ToolContext:
    return ToolContext(
        turn_id="turn-1", session_id="sess-1", language="zh", args={"query": query, **args}
    )


def _turn_flags():
    """跑一遍编排器的上下文计算（rag 挂载门就长在那儿）。"""
    message = Message(id="msg-1", session_id="sess-1", role="user", content="hi")
    return build_unified_context(
        TurnRequest(message="hi"),
        Session(id="sess-1", title="t"),
        message,
        [message],
        language="zh",
    ).tool_flags


RAG_ONLY = [RagSearchTool().definition]


# ================= 挂载门 =================


async def test_no_service_means_no_mount(tmp_home):
    """KB 服务没装配（独立测试路径）也不许报错，当作没有知识库。"""
    assert "rag" not in _turn_flags().context
    assert compute_mounted_tools(RAG_ONLY, _turn_flags()) == []


async def test_empty_kb_does_not_mount_rag(tmp_home):
    """建了库但一个文档都没有：搜不出东西，不挂。"""
    _service(tmp_home / "data")
    assert "rag" not in _turn_flags().context
    assert compute_mounted_tools(RAG_ONLY, _turn_flags()) == []


async def test_ready_kb_mounts_rag(tmp_path, tmp_home):
    service = _service(tmp_home / "data")
    await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)

    flags = _turn_flags()
    assert "rag" in flags.context
    assert compute_mounted_tools(RAG_ONLY, flags) == ["rag"]
    # context_gated 工具靠 forced 顶上来也认（用户显式点名）
    forced = flags.model_copy(update={"context": set(), "forced": {"rag"}})
    assert compute_mounted_tools(RAG_ONLY, forced) == ["rag"]


async def test_tool_mount_is_context_gated():
    assert RagSearchTool.definition.mount == ToolMount.CONTEXT_GATED
    assert RagSearchTool.definition.name == "rag"


# ================= 检索与 citation =================


async def test_rag_hit_carries_page_and_citation(tmp_path, tmp_home):
    service = _service(tmp_home / "data")
    kb, doc = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT, pages=2)

    result = await RagSearchTool().run(_ctx("傅里叶变换 频谱"))

    assert result.ok is True
    assert "《信号处理》" in result.output
    assert "信号处理.pdf" in result.output
    assert "第 1 页" in result.output or "第 2 页" in result.output

    sources = result.detail["sources"]
    assert sources
    top = CitationSource(**sources[0])  # 字段对不上会直接炸
    assert top.doc_id == doc.doc_id
    assert top.kb == kb.id
    assert top.page in (1, 2)
    assert top.snippet and top.snippet in result.output


async def test_rag_merges_all_ready_kbs(tmp_path, tmp_home):
    service = _service(tmp_home / "data")
    signal_kb, _ = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)
    bio_kb, _ = await _kb_with_doc(service, tmp_path, "生物笔记", BIO_TEXT)

    result = await RagSearchTool().run(_ctx("傅里叶变换 光合作用", top_k=5))

    assert {source["kb"] for source in result.detail["sources"]} == {signal_kb.id, bio_kb.id}
    assert "《信号处理》" in result.output and "《生物笔记》" in result.output


async def test_rag_no_hit_and_bad_args(tmp_home):
    """没有 ready 库 / 空 query 的兜底文案；模型瞎填 mode 不报错。"""
    _service(tmp_home / "data")  # 装配了服务，但一个库都没有

    empty = await RagSearchTool().run(_ctx("傅里叶变换"))
    assert empty.ok is True and "没有检索到" in empty.output

    blank = await RagSearchTool().run(_ctx("   "))
    assert blank.ok is False


async def test_rag_mode_falls_back_to_auto(tmp_path, tmp_home):
    service = _service(tmp_home / "data")
    await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)

    # 向量检索不设分数阈值，尾巴上的弱命中也会返回——由模型自己判断相关性
    unrelated = await RagSearchTool().run(_ctx("量子纠缠与超导磁悬浮"))
    assert unrelated.ok is True

    odd = await RagSearchTool().run(_ctx("傅里叶变换", mode="挖矿"))
    assert odd.ok is True and odd.detail["sources"]


async def test_rag_without_service_reports_plainly(tmp_home):
    result = await RagSearchTool().run(_ctx("傅里叶变换"))
    assert result.ok is False
    assert "没装配" in result.output


# ================= 引用进得了 done 事件 =================


async def test_citation_flows_into_done_payload(tmp_path, tmp_home):
    """工具 → outcome.citations → done.citations（前端引用面板读的字段）。"""
    service = _service(tmp_home / "data")
    kb, doc = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)

    llm = ScriptedLLM(
        [
            ScriptedStep(
                tool_calls=[LLMToolCall(id="c1", name="rag", arguments='{"query": "傅里叶变换"}')],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            ),
            ScriptedStep(
                chunks=["见《信号处理》。"], usage={"prompt_tokens": 8, "completion_tokens": 4}
            ),
        ]
    )
    bus = StreamBus("turn-1", session_id="sess-1")
    message = Message.new(session_id="sess-1", role="user", content="傅里叶变换是什么")
    ctx = UnifiedContext(session=SessionRef(id="sess-1"), capability="chat", message=message)
    deps = LoopDeps(
        client=llm,
        tools=ToolSet({"rag": RagSearchTool()}),
        model="scripted",
        provider="scripted",
    )
    events: list = []
    gen = bus.subscribe()

    async def _collect() -> None:
        async for event in gen:
            events.append(event)

    collector = asyncio.create_task(_collect())
    outcome = await run_agent_loop(
        ctx, bus, deps, [{"role": "user", "content": "傅里叶变换是什么"}]
    )
    bus.mark_closed()
    await collector

    assert outcome.final_text == "见《信号处理》。"
    assert len(outcome.citations) == 1
    citation = outcome.citations[0]
    assert (citation["doc_id"], citation["kb"], citation["page"]) == (doc.doc_id, kb.id, 1)
    # 收尾时这一步会被包成 DonePayload，字段对不上会炸——前端与落库都读它
    payload = DonePayload(response=outcome.final_text, citations=outcome.citations)
    assert payload.citations[0].kb == kb.id
    # 工具调用轨迹也落了账（前端工具卡）
    assert [call["tool_name"] for call in outcome.tool_calls] == ["rag"]
