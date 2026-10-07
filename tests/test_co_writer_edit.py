"""Co-Writer 改写管线：提示词拼装、工具开关与接地、轨迹合并、清洗、失败路径。"""

import pytest

from nnnu.capabilities import _shared
from nnnu.co_writer.edit import CoWriterOutputError, build_trace, context_text, run_edit
from nnnu.core.agent_loop import LoopOutcome
from nnnu.core.stream_bus import StreamBus
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.rag.base import Hit

pytestmark = pytest.mark.usefixtures("tmp_home", "repo_prompts")


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


@pytest.fixture(autouse=True)
def _tool_registry_ready():
    """这组用例不跑 app lifespan：手动装配内置工具注册表（幂等，照 main.py 同款）。"""
    from nnnu.runtime import bootstrap

    bootstrap.register_builtins()


BASE_KWARGS = dict(
    doc_id="cw-0a1b2c3d",
    selection="北京是中国的首都。",
    prefix="上一段文字。",
    suffix="下一段文字。",
    action="expand",
    instruction="写得更详细",
    language="zh",
)


async def _run(scripted, **overrides):
    install_scripted(lambda: scripted)
    return await run_edit(**{**BASE_KWARGS, **overrides})


def _tool_names(request):
    return {schema["function"]["name"] for schema in request.tools}


async def test_plain_edit_single_step():
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=["北京是中国的首都，", "也是政治与文化中心。"],
                usage={"prompt_tokens": 100, "completion_tokens": 20},
            )
        ]
    )
    outcome = await _run(scripted)
    assert outcome.edited == "北京是中国的首都，也是政治与文化中心。"
    assert outcome.stats["added"] == 1 and outcome.stats["deleted"] == 1
    assert outcome.trace == [] and outcome.citations == []
    assert outcome.degraded is None
    assert outcome.usage["tokens"] == 120
    assert outcome.model
    call = scripted.calls[0]
    # 没开联网、没勾库：只剩恒挂的 web_fetch
    assert _tool_names(call) == {"web_fetch"}
    assert "写作助手" in call.messages[0]["content"]
    user = call.messages[1]["content"]
    assert "【本次动作】扩写" in user
    assert "【用户要求】写得更详细" in user
    assert "【选中的文字】" in user and "北京是中国的首都。" in user
    assert "上一段文字。" in user and "下一段文字。" in user


async def test_web_switch_mounts_web_search():
    scripted = ScriptedLLM([ScriptedStep(chunks=["改好了。"])])
    await _run(scripted, use_web=True)
    assert _tool_names(scripted.calls[0]) == {"web_search", "web_fetch"}


async def test_rag_grounded_edit_merges_trace(monkeypatch):
    monkeypatch.setattr(
        _shared, "resolve_kb_names", lambda ctx: (["数学教材"], {"数学教材": "kb-11111111"})
    )

    class FakeKbService:
        async def search(self, kb_id, query, *, mode, top_k):
            return [
                Hit(
                    doc_id="kbdoc-11111111",
                    kb_id=kb_id,
                    score=1.0,
                    text="北京是中华人民共和国的首都。",
                    page=3,
                    metadata={"kb_name": "数学教材", "filename": "第一课.pdf"},
                )
            ]

    import nnnu.services.knowledge.service as kb_module

    monkeypatch.setattr(kb_module, "get_kb_service", lambda: FakeKbService())
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(
                        id="call-1",
                        name="rag",
                        arguments='{"query": "首都", "kb_name": "数学教材"}',
                    )
                ],
            ),
            ScriptedStep(
                chunks=["北京是中华人民共和国的首都，是政治文化中心。"],
                usage={"prompt_tokens": 50, "completion_tokens": 10},
            ),
        ]
    )
    outcome = await _run(scripted, kb_ids=["kb-11111111"])
    assert outcome.edited == "北京是中华人民共和国的首都，是政治文化中心。"
    assert len(outcome.trace) == 1
    entry = outcome.trace[0]
    assert entry["tool_name"] == "rag"
    assert entry["call_id"] == "call-1"
    assert entry["ok"] is True
    assert entry["args"] == {"query": "首都", "kb_name": "数学教材"}
    assert outcome.citations and outcome.citations[0]["kb"] == "kb-11111111"
    assert outcome.degraded is None
    # rag 的 kb_name 收口成本次挂载的库名；系统提示词带上知识库提示
    rag_schema = next(s for s in scripted.calls[0].tools if s["function"]["name"] == "rag")
    assert rag_schema["function"]["parameters"]["properties"]["kb_name"]["enum"] == ["数学教材"]
    assert "知识库" in scripted.calls[0].messages[0]["content"]


async def test_kb_selected_but_not_ready_degrades():
    # 不装配知识库服务：resolve_kb_names 拿不到任何库 → 如实降级
    scripted = ScriptedLLM([ScriptedStep(chunks=["改好了。"])])
    outcome = await _run(scripted, kb_ids=["kb-22222222"])
    assert outcome.degraded == "kb_not_ready"
    assert outcome.edited == "改好了。"


async def test_output_fences_cleaned():
    scripted = ScriptedLLM([ScriptedStep(chunks=["```markdown\n北京是中国的首都。\n```"])])
    outcome = await _run(scripted)
    assert outcome.edited == "北京是中国的首都。"


async def test_empty_model_output_raises():
    scripted = ScriptedLLM([ScriptedStep(chunks=[])])
    with pytest.raises(CoWriterOutputError):
        await _run(scripted)


async def test_english_language_renders_english_prompts():
    scripted = ScriptedLLM([ScriptedStep(chunks=["Beijing is the capital of China."])])
    outcome = await _run(
        scripted, language="en", selection="Beijing.", action="rewrite", instruction=""
    )
    call = scripted.calls[0]
    assert "[Action] rewrite" in call.messages[1]["content"]
    assert "writing assistant" in call.messages[0]["content"]
    assert outcome.edited == "Beijing is the capital of China."


def test_context_text_marks_truncation():
    assert context_text("前文", "后文") == "…前文\n后文…"
    assert context_text("", "") == ""
    assert context_text("  ", "") == ""


def test_build_trace_without_calls_is_empty():
    assert build_trace(LoopOutcome(), StreamBus(turn_id="turn-00000000")) == []
