"""rag 工具（§7.9）：挂载门、按选择检索、citation 对齐。

工具是「知识库 → 聊天」的唯一出口，这里盯四件事：
没选库时绝不挂载（默认不检索，对齐上游 DeepTutor）；选了库也只搜点名的那一个；
kb_name 缺失或不在已挂载列表里就报错（绝不猜默认值）；
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
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
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


def _ctx(query: str, *, kbs: dict[str, str] | None = None, **args) -> ToolContext:
    """kbs 是 chat 能力注入的 库名→kb_id 映射；不注入就等于没挂任何库。"""
    return ToolContext(
        turn_id="turn-1",
        session_id="sess-1",
        language="zh",
        metadata={"rag_kbs": kbs or {}},
        args={"query": query, **args},
    )


def _turn_flags(*kb_ids: str):
    """跑一遍编排器的上下文计算（rag 挂载门就长在那儿）。"""
    message = Message(id="msg-1", session_id="sess-1", role="user", content="hi")
    return build_unified_context(
        TurnRequest(message="hi", kb_ids=list(kb_ids)),
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


async def test_no_selection_means_no_mount(tmp_path, tmp_home):
    """库再多，没选就是不挂——默认不检索（§7.9）。"""
    service = _service(tmp_home / "data")
    await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)
    assert service.has_ready_kb() is True

    flags = _turn_flags()
    assert "rag" not in flags.context
    assert compute_mounted_tools(RAG_ONLY, flags) == []


async def test_selection_mounts_rag(tmp_home):
    """选了库就挂：纯列表判断，不回头查库状态（库删了/没建完由工具层兜底报错）。"""
    flags = _turn_flags("kb-信号")
    assert "rag" in flags.context
    assert compute_mounted_tools(RAG_ONLY, flags) == ["rag"]
    # context_gated 工具靠 forced 顶上来也认（用户显式点名）
    forced = flags.model_copy(update={"context": set(), "forced": {"rag"}})
    assert compute_mounted_tools(RAG_ONLY, forced) == ["rag"]


async def test_tool_mount_is_context_gated():
    assert RagSearchTool.definition.mount == ToolMount.CONTEXT_GATED
    assert RagSearchTool.definition.name == "rag"


# ================= 按选择检索与 citation =================


async def test_rag_hit_carries_page_and_citation(tmp_path, tmp_home):
    service = _service(tmp_home / "data")
    kb, doc = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT, pages=2)

    result = await RagSearchTool().run(
        _ctx("傅里叶变换 频谱", kbs={"信号处理": kb.id}, kb_name="信号处理")
    )

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


async def test_rag_searches_only_the_named_kb(tmp_path, tmp_home):
    """挂两个库、只点一个：结果里不许出现另一个库的任何片段。"""
    service = _service(tmp_home / "data")
    signal_kb, _ = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)
    bio_kb, _ = await _kb_with_doc(service, tmp_path, "生物笔记", BIO_TEXT)

    result = await RagSearchTool().run(
        _ctx(
            "傅里叶变换 光合作用",
            kbs={"信号处理": signal_kb.id, "生物笔记": bio_kb.id},
            kb_name="生物笔记",
            top_k=5,
        )
    )

    assert result.ok is True
    assert {source["kb"] for source in result.detail["sources"]} == {bio_kb.id}
    assert "《信号处理》" not in result.output


async def test_rag_no_hit_and_bad_args(tmp_path, tmp_home):
    """空 query / 缺 kb_name / 库不在列表 的兜底文案；模型瞎填 mode 不报错。"""
    service = _service(tmp_home / "data")
    kb, _ = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)
    kbs = {"信号处理": kb.id}

    blank = await RagSearchTool().run(_ctx("   ", kbs=kbs, kb_name="信号处理"))
    assert blank.ok is False and "query" in blank.output

    missing = await RagSearchTool().run(_ctx("傅里叶变换", kbs=kbs))
    assert missing.ok is False and "kb_name" in missing.output and "信号处理" in missing.output

    stranger = await RagSearchTool().run(_ctx("傅里叶变换", kbs=kbs, kb_name="量子力学"))
    assert stranger.ok is False and "不在已挂载列表" in stranger.output

    # 向量检索不设分数阈值，尾巴上的弱命中也会返回——由模型自己判断相关性
    unrelated = await RagSearchTool().run(_ctx("量子纠缠与超导", kbs=kbs, kb_name="信号处理"))
    assert unrelated.ok is True

    odd = await RagSearchTool().run(_ctx("傅里叶变换", kbs=kbs, kb_name="信号处理", mode="挖矿"))
    assert odd.ok is True and odd.detail["sources"]


async def test_rag_without_service_reports_plainly(tmp_home):
    result = await RagSearchTool().run(_ctx("傅里叶变换", kb_name="信号处理"))
    assert result.ok is False
    assert "没装配" in result.output


# ================= 端到端：选了库 → 系统提示 + 工具 schema =================


async def _rest_kb(client, tmp_path: Path, name: str, text: str) -> str:
    created = await client.post("/api/v1/kbs", json={"name": name})
    assert created.status_code == 200, created.text
    kb_id = created.json()["id"]
    upload = await client.post(
        f"/api/v1/kbs/{kb_id}/docs",
        files={"file": (f"{name}.pdf", _pdf(tmp_path / f"{name}.pdf", 1, text), "application/pdf")},
    )
    assert upload.status_code == 200, upload.text
    for _ in range(500):  # 构建是后台任务，轮询到 ready
        manifest = (await client.get(f"/api/v1/kbs/{kb_id}")).json()
        if manifest["status"] == "ready" and manifest["active_version"] > 0:
            return kb_id
        await asyncio.sleep(0.02)
    raise AssertionError("等待索引就绪超时")


def _recording_llm(captured: list[ScriptedLLM]):
    """ScriptedLLM 记录每次调用（calls[0] 就是本轮的系统提示与工具定义）。"""

    def factory() -> ScriptedLLM:
        llm = ScriptedLLM([ScriptedStep(chunks=["回答"])])
        captured.append(llm)
        return llm

    return factory


async def test_chat_turn_injects_kb_note_and_enum(client, tmp_path, repo_prompts):
    """REST 聊天带 kb_ids：系统提示点名库、rag 的 kb_name enum 收口到该库。"""
    embedding_service.install_embedding_stub(lambda: TopicEmbedder())
    kb_id = await _rest_kb(client, tmp_path, "信号处理", SIGNAL_TEXT)

    captured: list[ScriptedLLM] = []
    factory = _recording_llm(captured)
    install_scripted(factory)
    try:
        response = await client.post(
            "/api/v1/chat",
            json={"session_id": "sess-kb-e2e", "message": "傅里叶变换是什么", "kb_ids": [kb_id]},
        )
    finally:
        uninstall_scripted()
    assert response.status_code == 200

    request = captured[0].calls[0]
    system_prompt = request.messages[0]["content"]
    assert "已挂载知识库：信号处理" in system_prompt
    rag_schema = next(t for t in request.tools or [] if t["function"]["name"] == "rag")
    assert rag_schema["function"]["parameters"]["properties"]["kb_name"]["enum"] == ["信号处理"]


async def test_chat_without_selection_mounts_no_rag(client, tmp_path, repo_prompts):
    """有 ready 库但没选：rag 不挂、系统提示不提库（默认不检索知识库）。"""
    embedding_service.install_embedding_stub(lambda: TopicEmbedder())
    await _rest_kb(client, tmp_path, "信号处理", SIGNAL_TEXT)

    captured: list[ScriptedLLM] = []
    factory = _recording_llm(captured)
    install_scripted(factory)
    try:
        response = await client.post(
            "/api/v1/chat", json={"session_id": "sess-no-kb", "message": "傅里叶变换是什么"}
        )
    finally:
        uninstall_scripted()
    assert response.status_code == 200

    request = captured[0].calls[0]
    assert "已挂载知识库" not in request.messages[0]["content"]
    assert "rag" not in {t["function"]["name"] for t in request.tools or []}


# ================= kb_name 枚举每回合注入 =================


def test_kb_name_enum_comes_from_parameter_overrides():
    """enum 只作用在本回合的 ToolSet 上，注册表里的类级定义不许被改脏。"""
    tools = ToolSet(
        {"rag": RagSearchTool()},
        None,
        parameter_overrides={
            "rag": {"properties": {"kb_name": {"enum": ["信号处理", "生物笔记"]}}}
        },
    )
    parameters = tools.json_schemas()[0]["function"]["parameters"]
    assert parameters["properties"]["kb_name"]["enum"] == ["信号处理", "生物笔记"]
    # 没被覆盖的字段原样保留（merge 是浅合并，不是整体替换）
    assert set(parameters["required"]) == {"query", "kb_name"}
    assert RagSearchTool().definition.parameters["properties"]["kb_name"].get("enum") is None


# ================= 引用进得了 done 事件 =================


async def test_citation_flows_into_done_payload(tmp_path, tmp_home):
    """工具 → outcome.citations → done.citations（前端引用面板读的字段）。"""
    service = _service(tmp_home / "data")
    kb, doc = await _kb_with_doc(service, tmp_path, "信号处理", SIGNAL_TEXT)

    llm = ScriptedLLM(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(
                        id="c1",
                        name="rag",
                        arguments='{"query": "傅里叶变换", "kb_name": "信号处理"}',
                    )
                ],
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
    ctx = UnifiedContext(
        session=SessionRef(id="sess-1"),
        capability="chat",
        message=message,
        # 平时由 chat 能力注入（capability._resolve_kb_names）；这里手搓上下文就手填
        metadata={"rag_kbs": {"信号处理": kb.id}},
    )
    deps = LoopDeps(
        client=llm,
        tools=ToolSet(
            {"rag": RagSearchTool()},
            None,
            parameter_overrides={"rag": {"properties": {"kb_name": {"enum": ["信号处理"]}}}},
        ),
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
