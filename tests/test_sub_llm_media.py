"""次级 LLM 工具（brainstorm/reason）与图像生成（§7.2 / §6.9 次级成本并入回合）。"""

import base64

import httpx
import pytest

from nnnu.core.tool_protocol import ToolContext
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.media import service as media_service
from nnnu.services.settings.service import get_settings_service
from nnnu.tools.builtin.brainstorm import BrainstormTool
from nnnu.tools.builtin.media_gen_tool import ImageGenTool, VideoGenTool
from nnnu.tools.builtin.reason import ReasonTool

PNG_1PX = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


@pytest.fixture(autouse=True)
def clean_scripted():
    yield
    uninstall_scripted()


def _ctx(**args) -> ToolContext:
    tracker = CostTracker()
    return ToolContext(
        turn_id="turn-test",
        session_id="sess-test",
        metadata={"cost_tracker": tracker},
        args=dict(args),
    )


# ---- brainstorm / reason ----


async def test_brainstorm_calls_sub_llm_and_merges_cost():
    install_scripted(
        lambda: ScriptedLLM(
            [
                ScriptedStep(
                    chunks=["1. 想法甲：理由 A", "2. 想法乙：理由 B"],
                    usage={"prompt_tokens": 50, "completion_tokens": 30},
                )
            ]
        )
    )
    ctx = _ctx(topic="如何学习线性代数", num_ideas=2)
    result = await BrainstormTool().run(ctx)
    assert result.ok is True
    assert "想法甲" in result.output
    assert result.usage == {"prompt_tokens": 50, "completion_tokens": 30}
    tracker = ctx.metadata["cost_tracker"]
    summary = tracker.summary()
    assert summary["tokens"] == 80  # 次级调用用量已并入回合成本


async def test_reason_calls_sub_llm_with_effort():
    calls: list = []

    class RecordingScripted(ScriptedLLM):
        async def complete(self, request):
            calls.append(request)
            response = await super().complete(request)
            return response

    install_scripted(
        lambda: RecordingScripted(
            [
                ScriptedStep(
                    chunks=["结论：成立。"], usage={"prompt_tokens": 10, "completion_tokens": 5}
                )
            ]
        )
    )
    result = await ReasonTool().run(_ctx(question="1+1 是否等于 2", effort="high"))
    assert result.ok is True
    assert "结论" in result.output
    assert calls[0].reasoning_effort == "high"
    assert calls[0].tools is None


# ---- 图像生成 ----


async def test_imagegen_no_model_configured(tmp_home):
    result = await media_service.generate_image("示意图")
    assert result.ok is False
    assert "未配置" in result.error


async def test_imagegen_b64_saved_to_workspace(tmp_home):
    get_settings_service().save_area("models", {"image_model": "dall-e-3"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/generations")
        return httpx.Response(200, json={"data": [{"b64_json": PNG_1PX}]})

    result = await media_service.generate_image("一只猫", transport=httpx.MockTransport(handler))
    assert result.ok is True
    assert result.data_uri == f"data:image/png;base64,{PNG_1PX}"
    assert result.path and result.path.startswith("generated/image_")
    saved = tmp_home / "data" / "user" / "workspace" / result.path
    assert saved.is_file()


async def test_imagegen_tool_no_model_path(tmp_home):
    """工具层未配置模型时的报错面（成功路径在服务层用 MockTransport 覆盖）。"""
    get_settings_service().save_area("models", {"image_model": ""})
    result = await ImageGenTool().run(_ctx(prompt="一只猫"))
    assert result.ok is False
    assert "未配置" in result.output


async def test_videogen_returns_friendly_error():
    result = await VideoGenTool().run(_ctx(prompt="一段视频"))
    assert result.ok is False
    assert "尚未支持" in result.output
