"""DSML 解析：增量过滤、未闭合释放、参数类型强制。"""

from nnnu.core.dsml import DSMLStreamFilter, extract_dsml_tool_calls


def test_incremental_filter_strips_complete_calls():
    filt = DSMLStreamFilter()
    assert filt.feed("先说明<||DSML||function_calls>") == "先说明"
    assert filt.feed('<||DSML||invoke name="add">') == ""
    assert filt.feed('<||DSML||parameter name="a">1</||DSML||parameter>') == ""
    assert filt.feed("</||DSML||invoke></||DSML||function_calls>") == ""
    assert filt.feed("后说明") == "后说明"
    assert filt.flush() == ""


def test_partial_tag_buffered_until_closed():
    filt = DSMLStreamFilter()
    assert filt.feed("前<||DSML||invoke name=") == "前"
    assert filt.feed('"mul">{"a":') == ""
    assert filt.feed("2}</||DSML||invoke>后") == "后"
    assert filt.flush() == ""


def test_unclosed_flush_releases_original():
    filt = DSMLStreamFilter()
    assert filt.feed('正文<||DSML||invoke name="add">没写完') == "正文"
    # flush 原样释放残留（宁放原文不丢输出）
    assert filt.flush() == '<||DSML||invoke name="add">没写完'


def test_extract_complete_calls_and_clean_text():
    text = (
        "前置说明\n"
        '<||DSML||invoke name="add">{"a": 1, "b": 2}</||DSML||invoke>\n'
        "中间说明\n"
        '<||DSML||invoke name="mul">{"x": "3"}</||DSML||invoke>'
    )
    calls, cleaned = extract_dsml_tool_calls(
        text,
        {
            "add": {"properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
            "mul": {"properties": {"x": {"type": "integer"}}},
        },
    )
    assert len(calls) == 2
    assert calls[0].name == "add"
    assert calls[0].arguments == {"a": 1, "b": 2}
    assert calls[1].name == "mul"
    # "3" 按 schema 声明 integer 强制
    assert calls[1].arguments == {"x": 3}
    assert "invoke" not in cleaned
    assert "前置说明" in cleaned and "中间说明" in cleaned


def test_extract_parameter_form():
    text = (
        '<||DSML||invoke name="search">'
        '<||DSML||parameter name="q">傅里叶</||DSML||parameter>'
        '<||DSML||parameter name="limit">5</||DSML||parameter>'
        "</||DSML||invoke>"
    )
    calls, _ = extract_dsml_tool_calls(
        text, {"search": {"properties": {"limit": {"type": "integer"}}}}
    )
    assert len(calls) == 1
    assert calls[0].arguments == {"q": "傅里叶", "limit": 5}


def test_extract_no_schema_keeps_strings():
    text = '<||DSML||invoke name="t">{"n": "5"}</||DSML||invoke>'
    calls, _ = extract_dsml_tool_calls(text, None)
    assert calls[0].arguments == {"n": "5"}


def test_extract_no_calls_returns_empty():
    calls, cleaned = extract_dsml_tool_calls("纯文本没有调用", None)
    assert calls == []
    assert cleaned == "纯文本没有调用"
