"""提示词加载器：语言回退、占位符渲染、中英一致性校验。"""

from pathlib import Path

import pytest

from nnnu.services.i18n.prompts import PromptManager

FIXTURES = Path(__file__).parent / "fixtures" / "prompts"
REPO_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


@pytest.fixture
def manager() -> PromptManager:
    return PromptManager(prompts_root=FIXTURES)


def test_load_zh_file(manager):
    data = manager.load("sample", "zh")
    assert data["system"] == "你是 {name}，乐于助人的助手。"


def test_load_falls_back_to_en_when_zh_file_missing(manager):
    data = manager.load("only_en", "zh")
    assert data["system"] == "English only file."


def test_load_missing_everywhere_returns_empty(manager):
    assert manager.load("nope", "zh") == {}


def test_render_placeholder_substitution(manager):
    assert manager.render("sample", "zh", "system", name="小明") == "你是 小明，乐于助人的助手。"


def test_render_missing_key_falls_back_to_en(manager):
    # zh 的 sample.yaml 没有 stage_templates.planning → 回退 en
    assert manager.render("sample", "zh", "stage_templates.planning", task="T") == "Plan for T."


def test_render_failed_format_returns_original(manager):
    # 缺 name 参数 → str.format 抛 KeyError → 静默返回原文
    assert manager.render("sample", "zh", "system") == "你是 {name}，乐于助人的助手。"


def test_render_missing_everywhere_returns_empty_string(manager):
    assert manager.render("sample", "zh", "stage_templates.nope") == ""


def test_parity_reports_missing_zh_file(manager):
    assert "zh 缺少提示词文件 only_en" in manager.check_parity()


def test_parity_reports_missing_zh_key(manager):
    issues = manager.check_parity()
    assert "sample: zh 缺键 stage_templates.planning" in issues
    assert "sample: en 缺键 stage_templates.reasoning" in issues


def test_parity_empty_when_root_missing(tmp_path):
    manager = PromptManager(prompts_root=tmp_path / "prompts")
    assert manager.check_parity() == []


def test_repo_co_writer_keys_match_between_languages(repo_prompts):
    """真实 prompts/ 里的 co_writer.yaml：中英键集合一致（含嵌套 action_verb.*），
    且 edit.py 要用到的每条模板都渲染得出东西（防两边同时改名、静默渲染成空串）。

    repo_prompts 备用：以后若改走单例（get_prompt_manager）也不会跟着安装方式漂。
    """
    manager = PromptManager(prompts_root=REPO_PROMPTS)
    assert [issue for issue in manager.check_parity() if issue.startswith("co_writer")] == []
    keys = (
        "system",
        "action_template",
        "context_template",
        "user_template",
        "kb_note",
        "action_verb.rewrite",
        "action_verb.expand",
        "action_verb.shorten",
        "action_verb.translate",
        "action_verb.tone",
        "action_verb.free",
    )
    for lang in ("zh", "en"):
        for key in keys:
            rendered = manager.render(
                "co_writer",
                lang,
                key,
                tools="T",
                kb_note="K",
                instruction="I",
                action_verb="V",
                context="C",
                text="X",
                kbs="B",
            )
            assert rendered, (lang, key)
