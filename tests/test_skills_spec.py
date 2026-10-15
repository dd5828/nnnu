"""技能格式（§7.16）：SKILL.md 解析、校验与组装。"""

import pytest

from nnnu.services.skills.spec import (
    MAX_WHEN_TO_USE_CHARS,
    SkillFormatError,
    compose_skill_md,
    note_line,
    parse_skill_md,
)

VALID = compose_skill_md(
    "demo-skill",
    "示例技能：做演示用。",
    "第一步：先收集材料。\n第二步：再输出结论。",
    when_to_use="演示时使用",
)


def test_parse_round_trip():
    front, body = parse_skill_md(VALID)
    assert front.name == "demo-skill"
    assert front.description == "示例技能：做演示用。"
    assert front.when_to_use == "演示时使用"
    assert body.startswith("第一步")
    assert front.extra == {}


def test_extra_frontmatter_keys_are_kept():
    text = "---\nname: demo-skill\ndescription: 说明\ntags: [a, b]\nrequires:\n  sandbox: true\n---\n\n正文一段"
    front, _ = parse_skill_md(text)
    assert front.extra == {"tags": ["a", "b"], "requires": {"sandbox": True}}


def test_bom_and_crlf_tolerated():
    front, body = parse_skill_md("\ufeff" + VALID.replace("\n", "\r\n"))
    assert front.name == "demo-skill"
    assert "第一步" in body


def test_compose_escapes_tricky_characters():
    # 描述里带冒号/引号/换行，compose 走 YAML dump，parse 回来必须一字不差
    tricky = '带"引号"与:冒号\n还有换行'
    text = compose_skill_md("tricky-skill", tricky, "正文")
    front, _ = parse_skill_md(text)
    assert front.description == tricky


@pytest.mark.parametrize(
    "text",
    [
        "没有 frontmatter 的纯文本",
        "---\nname: demo-skill\n---\n\n正文",  # 缺 description
        "---\nname: demo-skill\ndescription: ''\n---\n\n正文",  # description 空
        "---\n[1, 2, 3]\n---\n\n正文",  # frontmatter 不是映射
        "---\nname: demo-skill\ndescription: 说明\n---\n\n   ",  # 正文空
        "---\nname: demo-skill\ndescription: 说明",  # 没有结束行
        "---\nname: demo-skill\ndescription: {a: b\n---\n\n正文",  # YAML 语法错
    ],
)
def test_parse_rejects_malformed(text):
    with pytest.raises(SkillFormatError):
        parse_skill_md(text)


@pytest.mark.parametrize(
    "name",
    ["Demo-Skill", "demo_skill", "1demo", "-demo", "a" * 49, "demo skill", ""],
)
def test_parse_rejects_bad_names(name):
    text = f"---\nname: {name}\ndescription: 说明\n---\n\n正文"
    with pytest.raises(SkillFormatError):
        parse_skill_md(text)


def test_parse_rejects_oversized_when_to_use():
    text = compose_skill_md(
        "demo-skill", "说明", "正文", when_to_use="啊" * (MAX_WHEN_TO_USE_CHARS + 1)
    )
    with pytest.raises(SkillFormatError):
        parse_skill_md(text)


def test_note_line_with_and_without_when_to_use():
    with_when = parse_skill_md(VALID)[0]
    assert note_line(with_when) == "- demo-skill：示例技能：做演示用。（何时用：演示时使用）"
    without = parse_skill_md(compose_skill_md("demo-skill", "说明", "正文"))[0]
    assert note_line(without) == "- demo-skill：说明"
