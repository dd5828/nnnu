"""mastery_path 能力（§7.5）：配置校验、工具面，以及**提示词里不许有状态**。

出题权归模型后，能力这一层只剩三件事：读配置、定路径、装 ask_user 缝（缝本身在
test_mastery_seam 里测）。状态改由模型每轮第一条 `mastery_status` 自己取——所以这里
最要紧的一条断言是**反过来的**：system 提示词的占位符里没有状态这一路，
服务端也就灌不进节点地图、更灌不进答案键（答案不外泄约束 ② 在能力层的守门处）。
"""

import re
from types import SimpleNamespace

import pytest

from nnnu.capabilities.mastery.capability import STAGE_TOOLS, MasteryCapability
from nnnu.services.i18n.prompts import get_prompt_manager

NEW_TOOLS = (
    "mastery_status",
    "mastery_quiz",
    "mastery_grade",
    "mastery_assess",
    "mastery_build",
    "mastery_paths",
    "mastery_switch",
    "mastery_leave",
)

# 提示词只许有这三个占位符：语言、工具清单、知识库小注
SYSTEM_VARS = {"language", "tools", "kb_note"}

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def _ctx(config: dict):
    return SimpleNamespace(config=config)


def test_stage_tools_are_the_eight():
    mounted = STAGE_TOOLS["responding"]
    for name in NEW_TOOLS:
        assert name in mounted
    # 旧的九动作单工具已退役，挂上去等于给模型一个空壳
    assert "mastery" not in mounted
    # 自己会发 LLM 调用的三个仍然排除在外（会吃脚本步数）
    for excluded in ("brainstorm", "reason", "consult_subagent"):
        assert excluded not in mounted


@pytest.mark.parametrize(
    "config, message",
    [
        ({"practice_count": "很多"}, "不是整数"),
        ({"practice_count": 9}, "超出范围"),
        ({"practice_count": 0}, "超出范围"),
    ],
)
def test_read_config_rejects_bad_practice_count(config, message):
    _spec, errors = MasteryCapability._read_config(_ctx(config))
    assert any(message in error for error in errors)


def test_read_config_defaults_and_strips():
    spec, errors = MasteryCapability._read_config(_ctx({"path_id": "  p-1  "}))
    assert errors == []
    assert spec["path_id"] == "p-1"
    assert spec["practice_count"] == 3


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_system_prompt_takes_no_state_vars(repo_prompts, lang):
    """system 提示词是静态的：占位符只有语言/工具/知识库小注，没有状态这一路。

    这条断言是「服务端不再塞状态块」的机械守门——谁想再把节点地图或答案键灌回去，
    就得先加占位符，而这里会当场红。上游同样是静态的
    （`deeptutor/capabilities/mastery/prompts/{zh,en}/system.md`，§16.2 白名单 A）。
    """
    raw = get_prompt_manager().load("mastery", lang)["system"]
    assert set(PLACEHOLDER.findall(raw)) == SYSTEM_VARS
    assert "state_block" not in raw  # 状态块的痕迹一点不留
