"""提示词加载器：语言回退、占位符渲染、中英一致性校验。"""

import re
from pathlib import Path

import pytest

from nnnu.book.models import BLOCK_TYPES
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


def test_repo_book_keys_match_between_languages(repo_prompts):
    """真实 prompts/ 里的 book.yaml：中英键集合一致，且每条模板渲染后没有任何残留
    占位符、双花括号也都被吃掉了——book.yaml 的 JSON 示例必须写成 {{ }}，否则
    str.format 抛错时 render 只会静默返回原文，这里就是那条防线。
    """
    manager = PromptManager(prompts_root=REPO_PROMPTS)
    assert [issue for issue in manager.check_parity() if issue.startswith("book")] == []
    leftover = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_.]*\}")
    # 有 system 提示词的块型：animation 走 math_animator 的提示词、note 零 LLM
    system_types = [item for item in BLOCK_TYPES if item not in ("animation", "note")]
    for lang in ("zh", "en"):
        catalog = manager.render("book", lang, "block_catalog")
        assert "flashcard" in catalog
        rendered_map = {
            "block_catalog": catalog,
            "spine.system": manager.render("book", lang, "spine.system", block_catalog=catalog),
            "spine.user": manager.render(
                "book", lang, "spine.user", title="T", refs_block="R", material="M"
            ),
            "spine.repair": manager.render("book", lang, "spine.repair", issues="I"),
            "blocks.common.user": manager.render(
                "book",
                lang,
                "blocks.common.user",
                chapter_title="T",
                content_type="theory",
                summary="S",
                objectives="O",
                material="M",
                focus="F",
                extra="",
            ),
            "blocks.repair": manager.render("book", lang, "blocks.repair", issues="I"),
            "blocks.animation.user": manager.render(
                "book", lang, "blocks.animation.user", chapter_title="T", focus="F", material="M"
            ),
            "page_chat.kb_note": manager.render("book", lang, "page_chat.kb_note", kbs="K"),
            "page_chat.system": manager.render(
                "book",
                lang,
                "page_chat.system",
                tools="T",
                kb_note="K",
                chapter_title="C",
                blocks="B",
                sources="S",
            ),
            "page_chat.user": manager.render("book", lang, "page_chat.user", question="Q"),
        }
        for block_type in system_types:
            rendered_map[f"blocks.{block_type}.system"] = manager.render(
                "book", lang, f"blocks.{block_type}.system"
            )
        for key, rendered in rendered_map.items():
            assert rendered, (lang, key)
            assert "{{" not in rendered and "}}" not in rendered, (lang, key)
            assert not leftover.search(rendered), (lang, key)
        # 块目录真的注入进了 system（渲染失败返回原文时这里会是空的）
        assert "flashcard" in rendered_map["spine.system"]
        # 各块系统提示词真认得自己的产出契约
        assert "JSON" in rendered_map["blocks.quiz.system"]
        assert "mermaid" in rendered_map["blocks.figure.system"]
