"""Co-Writer 行级 diff（纯展示）：op 序列与行数统计。"""

from nnnu.co_writer.diff import diff_lines, diff_stats


def test_diff_empty_to_nonempty():
    ops = diff_lines("", "第一行\n第二行")
    assert ops == [
        {"tag": "add", "text": "第一行"},
        {"tag": "add", "text": "第二行"},
    ]
    assert diff_stats(ops) == {"added": 2, "deleted": 0, "kept": 0}


def test_diff_both_empty():
    ops = diff_lines("", "")
    assert ops == []
    assert diff_stats(ops) == {"added": 0, "deleted": 0, "kept": 0}


def test_diff_identical_all_eq():
    text = "第一行\n第二行"
    ops = diff_lines(text, text)
    assert [op["tag"] for op in ops] == ["eq", "eq"]
    assert diff_stats(ops) == {"added": 0, "deleted": 0, "kept": 2}


def test_diff_replace_shows_del_then_add():
    ops = diff_lines("甲\n乙\n丙", "甲\n乙2\n丙")
    assert ops == [
        {"tag": "eq", "text": "甲"},
        {"tag": "del", "text": "乙"},
        {"tag": "add", "text": "乙2"},
        {"tag": "eq", "text": "丙"},
    ]
    assert diff_stats(ops) == {"added": 1, "deleted": 1, "kept": 2}


def test_diff_insert_in_middle():
    ops = diff_lines("甲\n丙", "甲\n乙\n丙")
    assert [op["tag"] for op in ops] == ["eq", "add", "eq"]


def test_diff_delete_lines():
    ops = diff_lines("甲\n乙\n丙", "甲\n丙")
    assert [op["tag"] for op in ops] == ["eq", "del", "eq"]


def test_diff_crlf_equals_lf():
    # 行尾风格换了不该显示成整篇改动
    ops = diff_lines("甲\r\n乙", "甲\n乙")
    assert [op["tag"] for op in ops] == ["eq", "eq"]


def test_diff_trailing_newline_is_not_a_change():
    ops = diff_lines("甲\n", "甲")
    assert ops == [{"tag": "eq", "text": "甲"}]
