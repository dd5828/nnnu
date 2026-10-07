"""块生成单测（§7.14 编译引擎的心脏）：各型产出的解析/校验/重试，不碰 DB 与路由。

主链是「洗不干净 → 带问题重试一次 → 再见就抛 BlockGenerationError」；figure 家族
另查围栏深校验（第一条围栏要对得上、mermaid 标签禁字符过不了确定性校验器）。
LLM 全走 ScriptedLLM：空脚本还调用会 RuntimeError，note 零 LLM 就借这条证明。
"""

import json

import pytest

from nnnu.book.blocks import BlockGenerationError, generate_block, max_tokens_for
from nnnu.book.models import Chapter
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

pytestmark = pytest.mark.usefixtures("repo_prompts")

USAGE = {"prompt_tokens": 120, "completion_tokens": 30}

QUIZ_JSON = json.dumps(
    {
        "stem": "傅里叶变换把信号变成了什么？",
        "options": [{"key": "A", "text": "频率成分"}, {"key": "B", "text": "时间戳"}],
        "answer_key": ["A"],
        "explanation": "它把信号拆成不同频率的正弦波。",
    },
    ensure_ascii=False,
)
GOOD_FIGURE = "频谱示意\n\n```mermaid\ngraph TD\n  A[时域信号] --> B[频域分解]\n```\n"
BAD_LABEL_FIGURE = "示意\n\n```mermaid\ngraph TD\n  A[swap arr[j]] --> B[done]\n```\n"


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _chapter() -> Chapter:
    return Chapter(
        key="ch-1",
        title="频率的世界",
        summary="讲频谱怎么看",
        objectives=["看懂频谱图"],
        content_type="theory",
    )


def _script(*texts: str) -> ScriptedLLM:
    scripted = ScriptedLLM([ScriptedStep(chunks=[text], usage=USAGE) for text in texts])
    install_scripted(lambda: scripted)
    return scripted


async def test_note_is_zero_llm():
    _script()  # 空脚本：真调用会 RuntimeError
    payload = await generate_block(block_type="note", chapter=_chapter())
    assert payload == {"markdown": ""}


async def test_quiz_json_parsed_normalized_and_tracked():
    scripted = _script("```json\n" + QUIZ_JSON + "\n```")
    tracker = CostTracker()
    payload = await generate_block(
        block_type="quiz", chapter=_chapter(), focus="小测一下", tracker=tracker
    )
    assert payload["stem"] == "傅里叶变换把信号变成了什么？"
    assert [option["key"] for option in payload["options"]] == ["A", "B"]
    assert payload["answer_key"] == ["A"]
    assert len(scripted.calls) == 1
    assert "出题人" in scripted.calls[0].messages[0]["content"]  # 中文系统提示词
    assert "小测一下" in scripted.calls[0].messages[1]["content"]  # 聚焦点进了用户消息
    assert max_tokens_for("quiz") > 0
    assert tracker.summary()["tokens"] > 0


async def test_text_unwraps_whole_fence():
    _script("```markdown\n### 开场\n\n正文一段。\n```")
    payload = await generate_block(block_type="text", chapter=_chapter())
    assert payload == {"markdown": "### 开场\n\n正文一段。"}


async def test_callout_variant_falls_back_to_info():
    _script(json.dumps({"variant": "古怪", "markdown": "要点"}, ensure_ascii=False))
    payload = await generate_block(block_type="callout", chapter=_chapter())
    assert payload == {"markdown": "要点", "variant": "info"}


async def test_figure_wrong_first_fence_retries_then_keeps_repair_hint():
    # 第一版把 html 围栏摆在最前（后面虽然有一张 mermaid，渲染层只认第一条）→ 第二版改对
    wrong_first = (
        "说明\n\n```html\n<div>不是它</div>\n```\n\n```mermaid\ngraph TD\n  A[甲] --> B[乙]\n```\n"
    )
    scripted = _script(wrong_first, GOOD_FIGURE)
    payload = await generate_block(block_type="figure", chapter=_chapter())
    assert "```mermaid" in payload["markdown"]
    assert len(scripted.calls) == 2
    retry_user = scripted.calls[1].messages[1]["content"]
    assert "上一次输出有以下问题" in retry_user  # 修复提示词进了第二次调用
    assert "只认" in retry_user  # 第一版的具体原因回了回去


async def test_figure_bad_mermaid_label_retries_then_gives_up():
    scripted = _script(BAD_LABEL_FIGURE, BAD_LABEL_FIGURE)
    with pytest.raises(BlockGenerationError) as excinfo:
        await generate_block(block_type="figure", chapter=_chapter())
    assert len(scripted.calls) == 2
    message = str(excinfo.value)
    assert "两次都没生成成功" in message
    assert "mermaid" in message  # 校验器的原因带进了错误里


async def test_json_block_garbage_retries_then_gives_up():
    scripted = _script("这里是散文，不是 JSON", "还是散文，没有对象")
    with pytest.raises(BlockGenerationError):
        await generate_block(block_type="flashcard", chapter=_chapter())
    assert len(scripted.calls) == 2


async def test_english_language_uses_english_prompts():
    scripted = _script(QUIZ_JSON)
    payload = await generate_block(block_type="quiz", chapter=_chapter(), language="en")
    assert payload["stem"]
    assert "You write quiz questions" in scripted.calls[0].messages[0]["content"]
