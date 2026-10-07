"""Book spine 生成：一次成、修复重试一次、软问题收下、引用收编、失败映射。"""

import json

import pytest

from nnnu.book.inputs import MaterialDigest, MaterialItem
from nnnu.book.llm import BookOutputError
from nnnu.book.spine import generate_spine
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

pytestmark = pytest.mark.usefixtures("repo_prompts")


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _digest() -> MaterialDigest:
    item = MaterialItem(
        kind="kb", ref="kbdoc-1", label="教材·第一章", text="傅里叶变换把信号拆成频率。"
    )
    text = item.render(1, {"kb": "知识库"})
    return MaterialDigest(items=[item], text=text, chars=len(text))


def _chapter(key: str, title: str, blocks: list[dict] | None = None, **extra) -> dict:
    chapter = {
        "key": key,
        "title": title,
        "summary": f"{title}的摘要",
        "objectives": ["知道傅里叶在干什么"],
        "content_type": "theory",
        "blocks_plan": blocks or [{"type": "text", "focus": "开场"}],
        "source_refs": [{"kind": "kb", "ref": "kbdoc-1", "label": "教材·第一章"}],
    }
    chapter.update(extra)
    return chapter


def _clean_book() -> dict:
    """三章齐全、三件套齐、概念图无悬空边——spine_issues 应为空。"""
    return {
        "chapters": [
            _chapter(
                "ch-1",
                "什么是傅里叶变换",
                [{"type": "text", "focus": "开场"}, {"type": "quiz", "focus": "小测"}],
            ),
            _chapter(
                "ch-2",
                "频域的世界",
                [{"type": "flashcard", "focus": "背名词"}, {"type": "figure", "focus": "画频谱"}],
            ),
            _chapter(
                "ch-3",
                "动手试试",
                [{"type": "text", "focus": "收尾"}, {"type": "deep_dive", "focus": "深入"}],
            ),
        ],
        "concept_graph": {
            "nodes": [
                {"id": "fourier", "label": "傅里叶变换", "group": "基础"},
                {"id": "spectrum", "label": "频谱", "group": "基础"},
            ],
            "edges": [{"source": "fourier", "target": "spectrum", "label": "分解出"}],
        },
    }


async def _run(scripted: ScriptedLLM, *, digest: MaterialDigest | None = None):
    install_scripted(lambda: scripted)
    tracker = CostTracker()
    outcome = await generate_spine(
        title="傅里叶入门", digest=digest or _digest(), language="zh", tracker=tracker
    )
    return outcome, tracker


async def test_generate_spine_clean_single_call():
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[json.dumps(_clean_book(), ensure_ascii=False)],
                usage={"prompt_tokens": 900, "completion_tokens": 300},
            )
        ]
    )
    outcome, tracker = await _run(scripted)
    assert len(scripted.calls) == 1
    assert outcome.issues == []
    assert [chapter.key for chapter in outcome.spine.chapters] == ["ch-1", "ch-2", "ch-3"]
    assert outcome.spine.language == "zh"
    assert outcome.spine.material_chars == _digest().chars
    assert outcome.spine.concept_graph.edges[0].source == "fourier"
    # 用量已进 tracker（服务侧拿它落 usage_records），outcome.usage 是同一份汇总
    assert tracker.summary()["tokens"] == 1200
    assert outcome.usage["tokens"] == 1200

    call = scripted.calls[0]
    system = call.messages[0]["content"]
    user = call.messages[1]["content"]
    # 块目录注入过了、JSON 示例的双花括号渲染回单花括号（没有残留占位符）
    assert "flashcard" in system and "{block_catalog}" not in system
    assert '"chapters"' in system
    assert "傅里叶变换把信号拆成频率" in user
    assert "kbdoc-1 → 教材·第一章" in user


async def test_soft_issues_trigger_one_repair_round():
    # 第一次：缺 flashcard（三件套不齐）；第二次：补齐
    first = _clean_book()
    first["chapters"][1]["blocks_plan"] = [{"type": "figure", "focus": "画频谱"}]
    second = _clean_book()
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[json.dumps(first, ensure_ascii=False)]),
            ScriptedStep(
                chunks=[json.dumps(second, ensure_ascii=False)],
                usage={"prompt_tokens": 900, "completion_tokens": 300},
            ),
        ]
    )
    outcome, _ = await _run(scripted)
    assert len(scripted.calls) == 2
    assert outcome.issues == []
    repair_prompt = scripted.calls[1].messages[1]["content"]
    assert "flashcard" in repair_prompt  # 问题清单进了修复提示
    assert "上一次输出" in repair_prompt


async def test_upstream_vocabulary_is_repaired():
    # 上游键名 learning_objectives 混进来 → 问题清单点名 → 修复重试
    bad = _clean_book()
    bad["chapters"][0]["learning_objectives"] = ["上游键名"]
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[json.dumps(bad, ensure_ascii=False)]),
            ScriptedStep(chunks=[json.dumps(_clean_book(), ensure_ascii=False)]),
        ]
    )
    outcome, _ = await _run(scripted)
    assert len(scripted.calls) == 2
    assert "learning_objectives" in scripted.calls[1].messages[1]["content"]
    assert outcome.issues == []


async def test_refs_outside_material_are_dropped_and_repaired():
    bad = _clean_book()
    bad["chapters"][0]["source_refs"] = [{"kind": "kb", "ref": "kbdoc-999", "label": "编的"}]
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[json.dumps(bad, ensure_ascii=False)]),
            ScriptedStep(chunks=[json.dumps(_clean_book(), ensure_ascii=False)]),
        ]
    )
    outcome, _ = await _run(scripted)
    assert len(scripted.calls) == 2
    assert "素材清单外" in scripted.calls[1].messages[1]["content"]
    assert outcome.issues == []
    assert outcome.spine.chapters[0].source_refs[0].ref == "kbdoc-1"


async def test_second_soft_failure_is_accepted_with_issues():
    bad = _clean_book()
    bad["chapters"][1]["blocks_plan"] = [{"type": "figure", "focus": "画频谱"}]  # 一直缺 flashcard
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[json.dumps(bad, ensure_ascii=False)]),
            ScriptedStep(chunks=[json.dumps(bad, ensure_ascii=False)]),
        ]
    )
    outcome, _ = await _run(scripted)
    assert len(scripted.calls) == 2
    assert any("flashcard" in issue for issue in outcome.issues)
    assert len(outcome.spine.chapters) == 3  # 收下不炸


async def test_bad_json_retries_once_then_raises():
    scripted = ScriptedLLM(
        [ScriptedStep(chunks=["这不是 JSON"]), ScriptedStep(chunks=["还是不给你 JSON"])]
    )
    with pytest.raises(BookOutputError):
        await _run(scripted)
    assert len(scripted.calls) == 2


async def test_json_in_fence_is_unwrapped():
    text = "```json\n" + json.dumps(_clean_book(), ensure_ascii=False) + "\n```"
    scripted = ScriptedLLM([ScriptedStep(chunks=[text])])
    outcome, _ = await _run(scripted)
    assert len(outcome.spine.chapters) == 3
