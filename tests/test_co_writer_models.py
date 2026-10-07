"""Co-Writer 纯函数：id 形状、标题/预览、切片校验、digest、模型输出清洗。"""

import pytest

from nnnu.co_writer.models import (
    MAX_SELECTION_CHARS,
    CoWriterDoc,
    CoWriterError,
    clean_model_output,
    content_digest,
    is_valid_doc_id,
    preview_of,
    title_of,
    validate_action,
    validate_content,
    validate_instruction,
    validate_selection,
)


def test_new_doc_id_shape():
    doc = CoWriterDoc.new()
    assert doc.id.startswith("cw-")
    assert is_valid_doc_id(doc.id)


@pytest.mark.parametrize(
    "bad", ["", "cw-1234567", "cw-123456789", "CW-AAAAAAAA", "cw-gggggggg", "../x"]
)
def test_is_valid_doc_id_rejects(bad):
    assert not is_valid_doc_id(bad)


def test_title_from_first_line_strips_decoration():
    assert title_of("", "# 我的论文\n正文") == "我的论文"
    assert title_of("  ## 标题  ", "正文") == "标题"


def test_title_explicit_wins_and_fallback():
    assert title_of("自定义", "# 首行") == "自定义"
    assert title_of("", "") == "未命名文档"
    assert title_of("", "\n\n   \n") == "未命名文档"


def test_title_truncates_with_ellipsis():
    title = title_of("", "好" * 500)
    assert len(title) == 120
    assert title.endswith("…")


def test_preview_first_line_and_truncation():
    assert preview_of("# 标题\n正文") == "标题"
    assert preview_of("") == ""
    long = preview_of("字" * 100)
    assert len(long) == 80
    assert long.endswith("…")


def test_validate_content_allows_empty_and_caps():
    assert validate_content("") == ""
    with pytest.raises(CoWriterError) as caught:
        validate_content("字" * 200_001)
    assert caught.value.code == "invalid_content"


def test_validate_selection_ok():
    validate_selection("abcdef", 1, 3, "bc")


def test_validate_selection_mismatch_is_doc_changed():
    with pytest.raises(CoWriterError) as caught:
        validate_selection("abcdef", 1, 3, "bX")
    assert caught.value.code == "doc_changed"


@pytest.mark.parametrize("start,end", [(-1, 2), (3, 3), (0, 99), (9, 10)])
def test_validate_selection_bad_range(start, end):
    with pytest.raises(CoWriterError) as caught:
        validate_selection("abcdef", start, end, "x")
    assert caught.value.code == "invalid_selection"


def test_validate_selection_empty_original():
    with pytest.raises(CoWriterError) as caught:
        validate_selection("abcdef", 1, 3, "")
    assert caught.value.code == "invalid_selection"


def test_validate_selection_caps_length():
    size = MAX_SELECTION_CHARS + 1
    content = "字" * size
    with pytest.raises(CoWriterError) as caught:
        validate_selection(content, 0, size, content)
    assert caught.value.code == "invalid_selection"


def test_digest_stable_and_distinguishing():
    assert content_digest("甲") == content_digest("甲")
    assert content_digest("甲") != content_digest("乙")
    assert len(content_digest("")) == 64


def test_validate_action_and_instruction():
    assert validate_action("expand") == "expand"
    with pytest.raises(CoWriterError) as caught:
        validate_action("nope")
    assert caught.value.code == "invalid_action"
    assert validate_instruction("rewrite", "   ") == ""
    with pytest.raises(CoWriterError) as caught:
        validate_instruction("free", "  ")
    assert caught.value.code == "invalid_action"
    with pytest.raises(CoWriterError):
        validate_instruction("rewrite", "长" * 501)


def test_clean_strips_fence_wrapper():
    assert clean_model_output("```markdown\n新文本\n```") == "新文本"
    assert clean_model_output("```\n新文本\n```") == "新文本"


def test_clean_keeps_content_that_itself_has_fence():
    original = "```python\nprint(1)\n```"
    assert clean_model_output(original, original=original) == original


def test_clean_strips_paired_quotes():
    assert clean_model_output('"新文本"') == "新文本"
    assert clean_model_output("“新文本”") == "新文本"
    assert clean_model_output("「新文本」") == "新文本"


def test_clean_keeps_quotes_the_original_had():
    original = "「引用」"
    assert clean_model_output(original, original=original) == original


def test_clean_leaves_unpaired_quotes_alone():
    assert clean_model_output('他说"你好') == '他说"你好'
    assert clean_model_output('"') == '"'
