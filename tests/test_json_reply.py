"""模型回复 → JSON 的提取与确定性修复（判分器与出题能力共用）。

要保住的四件事：
1. 代码块优先、整段兜底、多个代码块取最后一个；
2. LaTeX 的反斜杠：`\\frac` 被 JSON 当 `\f` 转义吃掉（换页符 + "rac"）、
   `\\beta` 变退格、`\\alpha` 直接解析失败——三种都得救回来；
3. 数学环境外的真换行/制表符是模型真要的排版，不能顺手改成 LaTeX 命令；
4. 救不回来的返回 None，交给调用方兜底，绝不猜内容。
"""

from nnnu.services.llm.json_reply import json_candidates, loads_lenient, parse_json_reply


def test_fenced_block_wins_over_prose():
    text = '想好了，如下：\n```json\n[{"stem": "1+1=?"}]\n```\n以上。'
    assert parse_json_reply(text) == [{"stem": "1+1=?"}]


def test_last_fenced_block_wins():
    text = '```json\n{"a": 1}\n```\n再改一版：\n```json\n{"a": 2}\n```\n'
    assert parse_json_reply(text) == {"a": 2}


def test_whole_text_and_bracket_slice_fallback():
    assert parse_json_reply('{"ok": true}') == {"ok": True}
    assert parse_json_reply('结果：{"ok": true} 就是这样') == {"ok": True}


def test_trailing_comma_survives():
    assert loads_lenient('[{"stem": "甲",},]') == [{"stem": "甲"}]


def test_latex_frac_restored_inside_math():
    # 模型写 "\frac"：JSON 把它当 \f 转义，解析出来是换页符 + "rac"
    assert parse_json_reply(r'[{"stem": "求 $\frac{1}{2}$ 的值"}]') == [
        {"stem": r"求 $\frac{1}{2}$ 的值"}
    ]


def test_latex_backslash_commands_restored():
    # \beta 变退格、\times 变制表符、\nabla 变换行——都在 $ 里，按 LaTeX 命令还原
    parsed = parse_json_reply(r'[{"stem": "$\beta$ 和 $\times$ 和 $\nabla$"}]')
    assert parsed == [{"stem": r"$\beta$ 和 $\times$ 和 $\nabla$"}]


def test_invalid_escape_doubled_not_dropped():
    # \alpha 不是合法 JSON 转义，整段会解析失败：翻倍后当字面量拿回来
    assert parse_json_reply(r'[{"stem": "$\alpha$ 粒子"}]') == [{"stem": r"$\alpha$ 粒子"}]


def test_upsilon_is_not_a_unicode_escape():
    # \upsilon 里的 \u 后面不是 4 位十六进制，不能当 unicode 转义
    assert parse_json_reply(r'[{"stem": "$\upsilon$"}]') == [{"stem": r"$\upsilon$"}]


def test_real_newline_outside_math_stays_newline():
    assert parse_json_reply('[{"stem": "第一行\n第二行"}]') == [{"stem": "第一行\n第二行"}]


def test_raw_control_char_inside_string():
    # 上游已经把 \f 解成真字符：JSON 里裸控制字符非法，得转义后再解析
    assert parse_json_reply('[{"stem": "A $\x0crac{1}{2}$ B"}]') == [{"stem": r"A $\frac{1}{2}$ B"}]


def test_nested_values_restored():
    parsed = parse_json_reply('{"questions": [{"stem": "$\x08eta$"}], "note": "$\x0crac{1}{2}$"}')
    assert parsed == {"questions": [{"stem": r"$\beta$"}], "note": r"$\frac{1}{2}$"}


def test_garbage_returns_none():
    assert parse_json_reply("模型今天不想回 JSON") is None
    assert parse_json_reply("") is None


def test_candidates_order_and_dedup():
    text = '前言\n```json\n{"a": 1}\n```\n'
    candidates = json_candidates(text)
    assert candidates[0] == '{"a": 1}'
    assert text.strip() in candidates
