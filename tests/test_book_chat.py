"""页聊天（§7.14）：提示词拼装（本页正文/来源/历史）、工具开关与接地、引用收集、
就绪降级与失败路径。装配与断言手法照 tests/test_co_writer_edit.py（伪上下文 + 脚本化 LLM）。
"""

import pytest

from nnnu.book.chat import BookChatOutputError, run_page_chat
from nnnu.book.models import Block, BookPage, Chapter, SourceRef
from nnnu.capabilities import _shared
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


def _page() -> BookPage:
    text = Block.new(block_type="text", payload={"markdown": "频域里横轴是频率，纵轴是振幅。"})
    text.status = "done"
    quiz = Block.new(
        block_type="quiz",
        payload={
            "stem": "频谱横轴看什么？",
            "options": [{"key": "A", "text": "频率"}, {"key": "B", "text": "时间"}],
            "answer_key": ["A"],
            "explanation": "横轴是频率。",
        },
    )
    quiz.status = "done"
    pending = Block.new(block_type="text", payload={"markdown": "还没定稿的旧草稿"})
    page = BookPage.new(book_id="bk-00000001", chapter_key="ch-1", page_no=1)
    page.blocks = [text, quiz, pending]
    return page


def _chapter() -> Chapter:
    return Chapter(
        key="ch-1",
        title="频域的世界",
        summary="频谱怎么读",
        objectives=["会看频谱图"],
        content_type="theory",
        blocks_plan=[{"type": "text", "focus": "开场"}],
        source_refs=[SourceRef(kind="kb", ref="kbdoc-1", label="教材库·第2章.md")],
    )


async def _run(scripted, **overrides):
    install_scripted(lambda: scripted)
    kwargs = dict(
        page=_page(),
        chapter=_chapter(),
        question="横轴是什么？",
        history=[],
        language="zh",
        kb_ids=[],
    )
    kwargs.update(overrides)
    return await run_page_chat(**kwargs)


def _tool_names(request):
    return {schema["function"]["name"] for schema in request.tools}


async def test_plain_chat_grounds_on_page_content():
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=["横轴是频率。"], usage={"prompt_tokens": 100, "completion_tokens": 20}
            )
        ]
    )
    outcome = await _run(scripted)
    assert outcome.answer == "横轴是频率。"
    assert outcome.citations == [] and outcome.degraded is None
    assert outcome.usage["tokens"] == 120 and outcome.model
    call = scripted.calls[0]
    # 没勾库、也不开联网搜索：只剩恒挂的 web_fetch（抓正文里给出的链接）
    assert _tool_names(call) == {"web_fetch"}
    system = call.messages[0]["content"]
    assert "伴读助手" in system
    assert "频域里横轴是频率，纵轴是振幅。" in system  # 本页 done 块的正文进了提示词
    assert (
        "频谱横轴看什么？" in system and "解析：横轴是频率。" in system
    )  # 测验带答案（助手该知道）
    assert "教材库·第2章.md" in system and "频域的世界" in system  # 章元信息与来源清单
    assert "还没定稿的旧草稿" not in system  # 没生成的块不进提示词
    assert call.messages[-1]["content"].strip() == "读者的提问：横轴是什么？"


async def test_history_is_prepended_before_question():
    scripted = ScriptedLLM([ScriptedStep(chunks=["接着讲。"])])
    history = [{"role": "user", "content": "上一问"}, {"role": "assistant", "content": "上一答"}]
    outcome = await _run(scripted, history=history)
    assert outcome.answer == "接着讲。"
    messages = scripted.calls[0].messages
    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "上一问" and messages[2]["content"] == "上一答"


async def test_rag_grounded_chat_collects_citations(monkeypatch):
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
                    text="频谱图的横轴是频率。",
                    page=2,
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
                        arguments='{"query": "频谱", "kb_name": "数学教材"}',
                    )
                ],
            ),
            ScriptedStep(
                chunks=["横轴是频率。"], usage={"prompt_tokens": 50, "completion_tokens": 10}
            ),
        ]
    )
    outcome = await _run(scripted, kb_ids=["kb-11111111"])
    assert outcome.answer == "横轴是频率。"
    assert outcome.citations and outcome.citations[0]["kb"] == "kb-11111111"
    assert outcome.degraded is None
    # rag 的 kb_name 收口成本次挂载的库名；系统提示词带上知识库提示
    rag_schema = next(s for s in scripted.calls[0].tools if s["function"]["name"] == "rag")
    assert rag_schema["function"]["parameters"]["properties"]["kb_name"]["enum"] == ["数学教材"]
    assert "已勾选这些知识库" in scripted.calls[0].messages[0]["content"]


async def test_kb_selected_but_not_ready_degrades():
    # 不装配知识库服务：resolve_kb_names 拿不到任何库 → 如实降级；rag 挂上了也是空枚举
    # （fail-closed：模型没得猜库名，系统提示词里也不带知识库提示）
    scripted = ScriptedLLM([ScriptedStep(chunks=["这页没提。"])])
    outcome = await _run(scripted, kb_ids=["kb-22222222"])
    assert outcome.degraded == "kb_not_ready"
    assert outcome.answer == "这页没提。"
    call = scripted.calls[0]
    assert _tool_names(call) == {"rag", "web_fetch"}
    rag_schema = next(s for s in call.tools if s["function"]["name"] == "rag")
    assert rag_schema["function"]["parameters"]["properties"]["kb_name"]["enum"] == []
    assert "已勾选这些知识库" not in call.messages[0]["content"]


async def test_empty_model_output_raises():
    scripted = ScriptedLLM([ScriptedStep(chunks=[])])
    with pytest.raises(BookChatOutputError):
        await _run(scripted)


async def test_english_language_renders_english_prompts():
    scripted = ScriptedLLM([ScriptedStep(chunks=["The horizontal axis is frequency."])])
    outcome = await _run(scripted, language="en")
    call = scripted.calls[0]
    assert "reading companion" in call.messages[0]["content"]
    assert call.messages[-1]["content"].strip() == "The reader asks: 横轴是什么？"
    assert outcome.answer == "The horizontal axis is frequency."
