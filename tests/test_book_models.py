"""Book 模型与纯校验：书名/素材、spine 收编与问题清单、十二类块 payload、id 形状。"""

import pytest

from nnnu.book import models
from nnnu.book.models import (
    BLOCK_TYPES,
    Block,
    Book,
    BookError,
    parse_spine,
    spine_issues,
    validate_block_payload,
    validate_sources,
    validate_title,
)


def _chapter(**overrides) -> dict:
    chapter = {
        "key": "ch-1",
        "title": "第一章",
        "summary": "讲点东西",
        "objectives": ["知道点东西"],
        "content_type": "theory",
        "blocks_plan": [
            {"type": "text", "focus": "开场"},
            {"type": "quiz", "focus": "小测"},
            {"type": "flashcard", "focus": "背名词"},
            {"type": "figure", "focus": "看图"},
        ],
        "source_refs": [{"kind": "kb", "ref": "kb-1", "label": "教材"}],
    }
    chapter.update(overrides)
    return chapter


def _spine(**overrides) -> dict:
    spine = {
        "chapters": [_chapter()],
        "concept_graph": {
            "nodes": [{"id": "n1", "label": "概念一", "group": "基础"}],
            "edges": [],
        },
    }
    spine.update(overrides)
    return spine


# ---- 书名与素材 ----


def test_validate_title_normalizes_and_rejects():
    assert validate_title("  我的书  ") == "我的书"
    for bad in ("", "   ", "字" * 121, "带\x00控制符"):
        with pytest.raises(BookError):
            validate_title(bad)


def test_validate_sources_requires_at_least_one():
    with pytest.raises(BookError) as caught:
        validate_sources({"kbs": [], "notebooks": [], "questions": None, "sessions": []})
    assert caught.value.code == "invalid_sources"
    with pytest.raises(BookError):
        validate_sources("这不是对象")


def test_validate_sources_normalizes_and_dedupes():
    sources = validate_sources(
        {
            "kbs": ["kb-1", "kb-1", "", 42, "kb-2"],
            "questions": {"filter": "wrong"},
        }
    )
    assert sources["kbs"] == ["kb-1", "kb-2"]
    assert sources["questions"] == {"filter": "wrong", "ids": []}
    assert sources["notebooks"] == []


def test_validate_sources_rejects_bad_question_filter():
    with pytest.raises(BookError):
        validate_sources({"questions": {"filter": "yesterday"}})
    with pytest.raises(BookError):
        validate_sources({"questions": {"filter": "ids", "ids": []}})


def test_book_new_shapes_and_id_prefix():
    book = Book.new(title="活书", sources={"kbs": ["kb-1"]})
    assert book.id.startswith("bk-")
    assert book.status == "draft"
    assert book.sources["kbs"] == ["kb-1"]


# ---- spine ----


def test_spine_issues_clean_example_is_empty():
    assert spine_issues(_spine()) == []
    parsed, issues = parse_spine(_spine())
    assert issues == []
    assert parsed.chapters[0].blocks_plan[2].type == "flashcard"
    assert parsed.concept_graph.nodes[0].label == "概念一"


def test_spine_issues_flags_upstream_vocabulary():
    # 上游 spine 的键名（learning_objectives / source_anchors）混进来要被点名
    raw = _spine()
    raw["chapters"] = [
        {
            "key": "ch-1",
            "title": "第一章",
            "summary": "x",
            "learning_objectives": ["上游键名"],
            "blocks_plan": [{"type": "text", "focus": "a"}, {"type": "quiz", "focus": "b"}],
        }
    ]
    issues = spine_issues(raw)
    assert any("learning_objectives" in issue for issue in issues)
    assert any("objectives" in issue for issue in issues)


def test_spine_issues_flags_missing_trio_and_unknown_type():
    raw = _spine()
    raw["chapters"][0]["blocks_plan"] = [
        {"type": "text", "focus": "a"},
        {"type": "podcast", "focus": "不存在的类型"},
    ]
    issues = spine_issues(raw)
    assert any("podcast" in issue for issue in issues)
    for required in ("quiz", "flashcard", "figure"):
        assert any(required in issue for issue in issues)


def test_spine_issues_graph_dangling_edge():
    raw = _spine()
    raw["concept_graph"]["edges"] = [{"source": "n1", "target": "n404", "label": "x"}]
    issues = spine_issues(raw)
    assert any("n404" in issue for issue in issues)


def test_parse_spine_repairs_and_truncates():
    raw = _spine(
        chapters=[
            _chapter(
                key="",
                title="",
                objectives=["o%d" % i for i in range(10)],
                blocks_plan=[{"type": "text", "focus": "f"}] * 12 + [{"type": "未知"}],
                source_refs=[{"kind": "manual", "ref": "x"}, {"kind": "kb", "ref": "kb-9"}],
            )
        ]
    )
    parsed, issues = parse_spine(raw)
    chapter = parsed.chapters[0]
    assert chapter.key == "ch-1"  # 缺 key 补一个
    assert chapter.title == "第 1 章"  # 缺 title 补一个
    assert len(chapter.objectives) == models.MAX_OBJECTIVES
    assert len(chapter.blocks_plan) == models.MAX_BLOCKS_PER_CHAPTER  # 未知类型丢掉后再截断
    # 上游 kind（manual）整条丢，我们认的 kb 留下
    assert [ref.kind for ref in chapter.source_refs] == ["kb"]
    assert issues  # 收编掉的都记了账


def test_parse_spine_hard_failures():
    with pytest.raises(BookError) as caught:
        parse_spine([])
    assert caught.value.code == "invalid_spine"
    with pytest.raises(BookError):
        parse_spine({"chapters": []})
    with pytest.raises(BookError):
        parse_spine({"chapters": [{"key": "ch-1", "blocks_plan": [{"type": "不存在"}]}]})


# ---- 块 payload ----


def _ok_payloads() -> dict[str, dict]:
    return {
        "text": {"markdown": "正文"},
        "callout": {"markdown": "提示", "variant": "warn"},
        "deep_dive": {"markdown": "深入", "sources": ["kb-1"]},
        "note": {"markdown": ""},
        "quiz": {
            "stem": "1+1=?",
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer_key": ["A"],
            "explanation": "显然",
        },
        "flashcard": {"cards": [{"front": "正面", "back": "背面"}]},
        "timeline": {"events": [{"when": "1999", "title": "发生", "detail": ""}]},
        "code": {"language": "python", "code": "print(1)", "explanation": ""},
        "figure": {"markdown": "看图\n```mermaid\ngraph TD; A-->B\n```"},
        "interactive_html": {"markdown": "试试\n```html\n<div>x</div>\n```"},
        "animation": {"markdown": "动画\n```nnnu-artifact\n{} \n```"},
        "concept_graph": {
            "nodes": [{"id": "n1", "label": "一"}, {"id": "n2", "label": "二"}],
            "edges": [{"source": "n1", "target": "n2"}],
        },
    }


def test_every_block_type_has_a_valid_example():
    payloads = _ok_payloads()
    assert set(payloads) == set(BLOCK_TYPES)  # 十二类一个不漏
    for block_type, payload in payloads.items():
        normalized = validate_block_payload(block_type, payload)
        assert isinstance(normalized, dict)


def test_block_new_shapes():
    block = Block.new(block_type="text", payload={"markdown": "hi"}, title="标题")
    assert block.id.startswith("blk-")
    assert block.status == "pending"
    with pytest.raises(BookError) as caught:
        Block.new(block_type="podcast")
    assert caught.value.code == "invalid_block_type"


def test_quiz_payload_strictness():
    with pytest.raises(BookError):
        validate_block_payload("quiz", {"stem": "x", "options": [{"key": "A", "text": "一"}]})
    with pytest.raises(BookError):
        # answer_key 不在选项里
        validate_block_payload("quiz", _ok_payloads()["quiz"] | {"answer_key": ["C"]})
    normalized = validate_block_payload(
        "quiz",
        {
            "stem": "x",
            "options": [{"key": "a", "text": "一"}, {"key": "B", "text": "二"}],
            "answer_key": ["a"],
            "explanation": "",
        },
    )
    assert normalized["options"][0]["key"] == "A"  # key 统一大写
    assert normalized["answer_key"] == ["A"]


def test_figure_family_requires_expected_fence():
    for block_type in ("figure", "interactive_html", "animation"):
        with pytest.raises(BookError):
            validate_block_payload(block_type, {"markdown": "没有围栏的正文"})
    with pytest.raises(BookError):
        # html 围栏不能满足 figure（各认各的那几条）
        validate_block_payload("figure", {"markdown": "```html\nx\n```"})
    with pytest.raises(BookError):
        validate_block_payload("callout", {"markdown": ""})


def test_concept_graph_payload_drops_dangling_edges():
    normalized = validate_block_payload(
        "concept_graph",
        {
            "nodes": [{"id": "n1", "label": "一"}, {"id": "n1", "label": "重复"}],
            "edges": [{"source": "n1", "target": "n404"}],
        },
    )
    assert len(normalized["nodes"]) == 1
    assert normalized["edges"] == []
    with pytest.raises(BookError):
        validate_block_payload("concept_graph", {"nodes": []})


def test_answer_key_written_as_string_is_rejected():
    # LLM 把数组写成字符串时按空处理（不能拿字符当元素逐个收下）→ 该块判无效
    with pytest.raises(BookError):
        validate_block_payload(
            "quiz",
            {
                "stem": "x",
                "options": [{"key": "A", "text": "一"}, {"key": "B", "text": "二"}],
                "answer_key": "AB",
                "explanation": "",
            },
        )
