"""visualize 三阶段流水线（§7.7）：分析 → 生成 → 复查，含校验/修复/降级/兜底。

脚本步数即 LLM 调用数：金路径三步（分析/生成/复查），修复与降级各多一步，
「多跑一轮就耗尽抛错」是天然校验。断言覆盖：
1. status 阶段序列（analyzing 会发两条：开始 + 选定的渲染类型）；
2. 正文契约 = 标题 + 说明 + 恰好一个渲染围栏；阶段原文（JSON/代码）不进正文；
3. done.response == content_done == 落库消息；
4. 校验不过修一次；echarts 再不过降级 SVG；其余类型垫兜底 HTML（正文带降级说明）；
5. 复查只能提交「过校验的完整代码」，不过就保留原版；
6. 非法配置快速失败不烧调用；用户 pin 的类型覆盖模型判断；
7. manim_video：本机无依赖时友好报错（分析段系统提示里已劝退）。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.capabilities.visualize.capability import STAGE_TOOLS
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.render.validation import extract_artifact_json

QUESTION = "画一条正弦曲线"

ANALYSIS_SVG = (
    '{"render_type": "svg", "title": "正弦曲线", "description": "一条正弦波的示意图。",'
    ' "elements": ["坐标轴", "正弦曲线"], "rationale": "静态图够用"}'
)
ANALYSIS_ECHARTS = (
    '{"render_type": "echarts", "title": "正弦曲线", "description": "正弦波数据图。",'
    ' "elements": ["折线"], "rationale": "定量数据"}'
)
ANALYSIS_MERMAID = (
    '{"render_type": "mermaid", "title": "正弦曲线", "description": "画个流程。",'
    ' "elements": ["节点"], "rationale": "结构化"}'
)
ANALYSIS_MANIM = (
    '{"render_type": "manim_video", "title": "正弦波动画", "description": "波形生成动画。",'
    ' "elements": ["波形"], "rationale": "用户要动画"}'
)

GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
    '<path d="M0 25 Q25 0 50 25 T100 25" /></svg>'
)
BAD_SVG = "<svg><circle></svg>"
BAD_ECHARTS = "{series: [1, 2]}"
STILL_BAD_ECHARTS = '{"title": {"text": "没有 series"}}'
GOOD_MERMAID = "flowchart TD\n  A[开始] --> B[结束]"
REVIEW_OK = '{"verdict": "ok", "code": "", "notes": "没问题"}'

# manim 支线的动画流水线素材（六阶段原文与最终代码）
ANIM_CODE = (
    "from manim import *\n\n\nclass WaveScene(Scene):\n"
    "    def construct(self):\n        self.wait(2)\n"
)

# 提示词 YAML 里的区分串
ANALYZE_MARK = "【分析阶段】"
GENERATE_MARK = "【生成阶段】"
REVIEW_MARK = "【复查阶段】"
MANIM_DISCOURAGE_MARK = "禁止**选择 manim_video"
STAGE_KEYS = ("analyzing", "generating", "reviewing")


@pytest.fixture
def ws_client(tmp_home):
    """TestClient 跑 lifespan（数据库/运行时/渲染服务装配在 portal 线程事件循环）。"""
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def ws_client_mock_render(tmp_home, repo_prompts, monkeypatch):
    """假渲染器版：manim 支线也走到产物（env 必须在 lifespan 装配渲染服务之前设好）。"""
    monkeypatch.setenv("NNNU_MANIM_MOCK", "1")
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _receive_until(ws, predicate, limit=200) -> list[dict]:
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"未等到目标事件，已收 {len(events)} 条")


def _run_visualize(ws, *, message=QUESTION, config=None, session_id="sess-visualize") -> None:
    payload: dict = {
        "type": "chat",
        "session_id": session_id,
        "message": message,
        "capability": "visualize",
        "language": "zh",
    }
    if config is not None:
        payload["config"] = config
    ws.send_json(payload)


def _wait_turn_settled(ws_client, session_id: str) -> None:
    runtime = ws_client.app.state.runtime
    for _ in range(100):
        if runtime.active_turn_for(session_id) is None:
            return
        time.sleep(0.05)
    raise AssertionError("回合未在预期时间内收尾")


def _stages_of(events: list[dict]) -> list[str]:
    return [e["payload"]["stage"] for e in events if e["type"] == "status"]


def _status_messages(events: list[dict]) -> list[str]:
    return [e["payload"]["message"] for e in events if e["type"] == "status"]


def _user_text(call) -> str:
    return next(m["content"] for m in reversed(call.messages) if m["role"] == "user")


def _tool_names(call) -> set[str]:
    return {schema["function"]["name"] for schema in call.tools}


def _script(*texts: str) -> ScriptedLLM:
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    return ScriptedLLM([ScriptedStep(chunks=[text], usage=usage) for text in texts])


def _fence(text: str, lang: str) -> str:
    return f"```{lang}\n{text}\n```"


async def test_visualize_golden_path_svg(ws_client, repo_prompts):
    scripted = _script(ANALYSIS_SVG, _fence(GOOD_SVG, "svg"), REVIEW_OK)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    # ① 阶段序列：analyzing 发两条（开始 + 选定类型），生成、复查各一条
    assert _stages_of(events) == ["analyzing", "analyzing", "generating", "reviewing"]
    messages = _status_messages(events)
    assert "渲染类型：svg" in messages[1]

    # ② 正文契约：标题 + 说明 + 恰好一个 svg 渲染围栏；阶段原文不进正文
    assert events[-1]["type"] == "done"
    response = events[-1]["payload"]["response"]
    assert "## 正弦曲线" in response
    assert "一条正弦波的示意图。" in response
    assert _fence(GOOD_SVG, "svg") in response
    assert response.count("```") == 2  # 恰好一个围栏
    assert ANALYSIS_SVG not in response
    assert REVIEW_OK not in response

    # ② 续：内容被吞（无 content_delta，只有收尾的那次 content_done）
    assert not any(e["type"] == "content_delta" for e in events)
    content_done = [e for e in events if e["type"] == "content_done"]
    assert content_done and content_done[-1]["payload"]["full_text"] == response

    # ③ 三阶段各一轮；系统提示各就各位
    assert scripted.exhausted
    assert len(scripted.calls) == 3
    assert ANALYZE_MARK in scripted.calls[0].messages[0]["content"]
    assert GENERATE_MARK in scripted.calls[1].messages[0]["content"]
    assert REVIEW_MARK in scripted.calls[2].messages[0]["content"]
    # 分析段系统提示里带了「本机没有 manim」的劝退（渲染服务可用性检查生效）
    assert MANIM_DISCOURAGE_MARK in scripted.calls[0].messages[0]["content"]
    # 生成段的用户消息里带着分析简报
    assert "一条正弦波的示意图。" in _user_text(scripted.calls[1])

    # 工具白名单：分析段可检索、生成段为空
    assert _tool_names(scripted.calls[0]) <= set(STAGE_TOOLS["analyzing"])
    assert _tool_names(scripted.calls[1]) == set()

    # ④ cost_summary 恰好一次；落库 = done.response；能力粘在会话上
    assert sum(1 for e in events if e["type"] == "cost_summary") == 1
    session_id = events[0]["session_id"]
    _wait_turn_settled(ws_client, session_id)
    detail = ws_client.get(f"/api/v1/sessions/{session_id}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[-1]["content"] == response
    assert detail["capability"] == "visualize"


async def test_visualize_repairs_bad_code_once(ws_client, repo_prompts):
    """生成不过校验 → 修一次 → 修好的代码过闸门进正文（总 4 次调用）。"""
    fixed = _fence(GOOD_SVG, "svg")
    scripted = _script(ANALYSIS_SVG, _fence(BAD_SVG, "svg"), fixed, REVIEW_OK)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    assert GOOD_SVG in response
    assert BAD_SVG not in response
    assert len(scripted.calls) == 4
    # 修复提示词拿到了校验错误原文
    assert "校验错误" in _user_text(scripted.calls[2])


async def test_visualize_echarts_downgrades_to_svg(ws_client, repo_prompts):
    """echarts 两次不过 → 按 §7.7 强制 SVG 重画一次，不算降级兜底（还会走复查）。"""
    scripted = _script(
        ANALYSIS_ECHARTS,
        _fence(BAD_ECHARTS, "echarts"),
        _fence(STILL_BAD_ECHARTS, "echarts"),
        _fence(GOOD_SVG, "svg"),
        REVIEW_OK,
    )
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    assert _fence(GOOD_SVG, "svg") in response
    assert "```echarts" not in response
    assert "兜底" not in response  # 不是兜底页，是正经重画
    assert _stages_of(events) == ["analyzing", "analyzing", "generating", "reviewing"]
    assert len(scripted.calls) == 5
    # 重画提示词点名「不要再用 echarts」
    assert "不要再用 echarts" in _user_text(scripted.calls[3])


async def test_visualize_falls_back_to_html_when_hopeless(ws_client, repo_prompts):
    """mermaid 两次不过且不在降级名单 → 兜底 HTML + 正文降级说明；不再复查。"""
    scripted = _script(
        ANALYSIS_MERMAID,
        _fence("画个图：\nflowchart TD", "mermaid"),
        _fence("还是不对", "mermaid"),
    )
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    assert "```html" in response
    assert "兜底" in response
    assert _stages_of(events) == ["analyzing", "analyzing", "generating"]  # 没有复查
    assert len(scripted.calls) == 3


async def test_visualize_review_fix_only_accepted_when_valid(ws_client, repo_prompts):
    """复查说 fixed 但给的代码不过校验 → 保留原版。"""
    review_fixed_bad = '{"verdict": "fixed", "code": "<svg><未闭合></svg>", "notes": "改了"}'
    scripted = _script(ANALYSIS_SVG, _fence(GOOD_SVG, "svg"), review_fixed_bad)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    assert GOOD_SVG in response
    assert "未闭合" not in response


async def test_visualize_review_fix_accepted_when_valid(ws_client, repo_prompts):
    """复查提交的完整代码过校验 → 采纳替换。"""
    better_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60">'
        '<path d="M0 30 Q30 0 60 30 T120 30" /></svg>'
    )
    review_fixed = json.dumps(
        {"verdict": "fixed", "code": better_svg, "notes": "换了曲线"}, ensure_ascii=False
    )
    scripted = _script(ANALYSIS_SVG, _fence(GOOD_SVG, "svg"), review_fixed)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    assert better_svg in response
    assert GOOD_SVG not in response


async def test_visualize_pinned_type_overrides_model(ws_client, repo_prompts):
    """用户 pin 了 mermaid：模型分析说 svg 也按 mermaid 出。"""
    scripted = _script(ANALYSIS_SVG, _fence(GOOD_MERMAID, "mermaid"), REVIEW_OK)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws, config={"render_type": "mermaid"})
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    assert _fence(GOOD_MERMAID, "mermaid") in response
    assert _status_messages(events)[1].startswith("渲染类型：mermaid")
    # 分析段的用户消息里点了名（pin_note 生效）
    assert "用户已指定渲染类型为 mermaid" in _user_text(scripted.calls[0])


async def test_visualize_manim_video_without_deps_fails_friendly(ws_client, repo_prompts):
    """分析段判出动画但本机没有 Manim：友好报错收尾，不再烧生成调用。"""
    scripted = _script(ANALYSIS_MANIM)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    message = errors[-1]["payload"]["message"]
    assert "缺少数学动画依赖" in message
    assert "math-animator" in message and "ffmpeg" in message
    assert not any(e["type"] == "done" for e in events)
    assert len(scripted.calls) == 1  # 生成与复查都没跑


async def test_visualize_manim_branch_runs_animation(ws_client_mock_render):
    """分析段判出 manim_video 且依赖可用：转六阶段流水线，正文是动画产物契约。"""
    scripted = _script(
        ANALYSIS_MANIM,
        '{"learning_goal": "正弦波动画", "math_focus": ["正弦"], "visual_targets": ["波形"],'
        ' "narrative_steps": ["画波形"]}',
        '{"title": "正弦波动画", "scene_outline": ["画波形"], "visual_style": "简洁",'
        ' "animation_notes": [], "code_constraints": []}',
        _fence(ANIM_CODE, "python"),
        '{"summary_text": "正弦波生成动画。", "key_points": ["正弦"]}',
    )
    install_scripted(lambda: scripted)
    with ws_client_mock_render.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    assert events[-1]["type"] == "done"
    response = events[-1]["payload"]["response"]
    payload = extract_artifact_json(response)
    assert payload is not None and payload["mime"] == "video/mp4"
    assert ANIM_CODE in response
    assert "正弦波生成动画。" in response

    # 走的是动画六阶段，不是文本渲染的 generating/reviewing
    stages = _stages_of(events)
    assert "concept_analysis" in stages and "render_output" in stages
    assert "generating" not in stages and "reviewing" not in stages
    assert not any(e["type"] == "error" for e in events)
    assert scripted.exhausted and len(scripted.calls) == 5


async def test_visualize_invalid_config_fails_fast(ws_client):
    """非法 render_type：报 error(recoverable=false)，一次 LLM 调用都不烧。"""
    created = []

    def factory():
        client = _script(ANALYSIS_SVG)
        created.append(client)
        return client

    install_scripted(factory)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_visualize(ws, config={"render_type": "pie"})
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    assert "pie" in errors[-1]["payload"]["message"]
    assert created == []
    assert not any(e["type"] == "status" for e in events)
