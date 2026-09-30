"""math_animator 六阶段流水线（§7.8）：分析 → 设计 → 生成 → 渲染+修复 → 总结 → 输出。

渲染走假渲染器（NNNU_MANIM_MOCK=1，夹具里设）——真 Manim 渲染是 P7 遗留。
脚本步数即 LLM 调用数，断言覆盖：
1. 金路径：六阶段状态齐全、正文契约（标题+总结+artifact 围栏+python 源码+日志节选）、
   artifact URL 真能下到字节、落库 = done.response、恰好一次 cost_summary；
2. 渲染失败进 code_retry：注入 NNNU_MOCK_FAIL 必失败一次，修复后第二轮成功（attempts=2）；
3. 连续失败：总渲染尝试 ≤4（1 次生成 + 最多 3 次修复），失败也走 done——
   正文给失败说明与最后一版代码，不是 error 事件；
4. 修复产出抠不出代码块 → 立即停（拿同一版再渲染必然再失败）；
5. 代码里没有场景类：本地就判掉，不进渲染进程，直接进修复；
6. 缺依赖（无 manim/ffmpeg）：友好 error + 安装指引，0 次 LLM 调用；
7. 非法 quality：快速失败，0 次 LLM 调用。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.render.service import MOCK_FAIL_MARKER, MOCK_PLACEHOLDER_BYTES
from nnnu.services.render.validation import extract_artifact_json

QUESTION = "做一个傅里叶级数的可视化动画"

ANALYSIS = json.dumps(
    {
        "learning_goal": "演示方波如何由正弦波叠加逼近",
        "math_focus": ["傅里叶级数", "谐波叠加"],
        "visual_targets": ["方波", "逐项叠加的正弦波"],
        "narrative_steps": ["画出方波", "叠加前三项", "展示逼近效果"],
    },
    ensure_ascii=False,
)
DESIGN = json.dumps(
    {
        "title": "傅里叶级数演示",
        "scene_outline": ["画出方波", "逐项叠加正弦波"],
        "visual_style": "深色背景、简洁配色",
        "animation_notes": ["每步停顿"],
        "code_constraints": ["对象不重叠"],
    },
    ensure_ascii=False,
)
SUMMARY = json.dumps(
    {"summary_text": "把方波的傅里叶逼近过程做成了动画。", "key_points": ["方波"]},
    ensure_ascii=False,
)

GOOD_CODE = (
    "from manim import *\n\n\nclass FourierScene(Scene):\n"
    "    def construct(self):\n        self.wait(2)\n"
)
FAIL_CODE = GOOD_CODE.replace("from manim import *", f"from manim import *  # {MOCK_FAIL_MARKER}")
NO_SCENE_CODE = "import math\n\nprint('没有场景类')\n"

# 提示词 YAML 里的区分串（阶段原文被吞掉，只能从系统/用户消息里认出各阶段）
ANALYSIS_MARK = "【概念分析】"
DESIGN_MARK = "【方案设计】"
GENERATE_MARK = "【代码生成】"
RETRY_MARK = "【代码修复】"
SUMMARY_MARK = "【总结】"
STAGE_ORDER = [
    "concept_analysis",
    "concept_design",
    "code_generation",
    "code_retry",
    "summary",
    "render_output",
]


@pytest.fixture
def ws_client(tmp_home, repo_prompts, monkeypatch):
    """假渲染器 + 临时 home 的 TestClient（env 必须在 lifespan 装配渲染服务之前设好）。"""
    monkeypatch.setenv("NNNU_MANIM_MOCK", "1")
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def ws_client_real_deps(tmp_home, repo_prompts, monkeypatch):
    """不设 NNNU_MANIM_MOCK：走真渲染服务的依赖体检（本机没装 manim/ffmpeg）。"""
    monkeypatch.delenv("NNNU_MANIM_MOCK", raising=False)
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _receive_until(ws, predicate, limit=300) -> list[dict]:
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"未等到目标事件，已收 {len(events)} 条")


def _run_animator(ws, *, message=QUESTION, config=None, session_id="sess-anim") -> None:
    payload: dict = {
        "type": "chat",
        "session_id": session_id,
        "message": message,
        "capability": "math_animator",
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


def _fence(text: str, lang: str = "python") -> str:
    return f"```{lang}\n{text}\n```"


def _script(*texts: str) -> ScriptedLLM:
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    return ScriptedLLM([ScriptedStep(chunks=[text], usage=usage) for text in texts])


async def test_math_animator_golden_path(ws_client):
    """金路径：4 次 LLM 调用跑完六阶段，产出可下载的视频产物。"""
    scripted = _script(ANALYSIS, DESIGN, _fence(GOOD_CODE), SUMMARY)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    # ① 六阶段按序出现（同一阶段可能发多条状态，去重后看顺序）
    stages = _stages_of(events)
    assert sorted(set(stages), key=stages.index) == STAGE_ORDER
    assert any("第 1/4 次尝试" in m for m in _status_messages(events))

    # ② 正文契约：标题 + 总结 + artifact 围栏 + python 源码 + 日志节选
    assert events[-1]["type"] == "done"
    response = events[-1]["payload"]["response"]
    assert response.startswith("## 傅里叶级数演示")
    assert "把方波的傅里叶逼近过程做成了动画。" in response
    payload = extract_artifact_json(response)
    assert payload is not None
    assert payload["url"].startswith("/api/v1/renders/rnd-")
    assert payload["filename"] == "animation.mp4"
    assert payload["mime"] == "video/mp4"
    assert payload["kind"] == "video"
    assert payload["attempts"] == 1
    assert GOOD_CODE in response
    assert "[mock]" in response  # 日志节选

    # ② 续：阶段原文被吞（无 content_delta），content_done 就是落库正文
    assert not any(e["type"] == "content_delta" for e in events)
    content_done = [e for e in events if e["type"] == "content_done"]
    assert content_done and content_done[-1]["payload"]["full_text"] == response

    # ③ 产物真的下得到（正文围栏 → HTTP 通道闭环）
    fetched = ws_client.get(payload["url"])
    assert fetched.status_code == 200
    assert fetched.content == MOCK_PLACEHOLDER_BYTES
    assert fetched.headers["content-type"].startswith("video/mp4")

    # ④ 每次调用各就各位
    assert scripted.exhausted and len(scripted.calls) == 4
    systems = [call.messages[0]["content"] for call in scripted.calls]
    assert ANALYSIS_MARK in systems[0]
    assert DESIGN_MARK in systems[1]
    assert GENERATE_MARK in systems[2]
    assert SUMMARY_MARK in systems[3]
    assert "傅里叶级数" in _user_text(scripted.calls[1])  # 分析原文喂进设计段

    # ⑤ cost_summary 恰好一次；落库 = done.response；能力粘在会话上
    assert sum(1 for e in events if e["type"] == "cost_summary") == 1
    session_id = events[0]["session_id"]
    _wait_turn_settled(ws_client, session_id)
    detail = ws_client.get(f"/api/v1/sessions/{session_id}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[-1]["content"] == response
    assert detail["capability"] == "math_animator"


async def test_math_animator_repairs_after_injected_failure(ws_client):
    """注入 NNNU_MOCK_FAIL → 渲染失败一次 → 修复 → 第二次成功（attempts=2）。"""
    scripted = _script(ANALYSIS, DESIGN, _fence(FAIL_CODE), _fence(GOOD_CODE), SUMMARY)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    messages = _status_messages(events)
    assert any("第 1/4 次尝试" in m for m in messages)
    assert any("渲染失败" in m and "修一版" in m for m in messages)
    assert any("第 2/4 次尝试" in m for m in messages)

    response = events[-1]["payload"]["response"]
    payload = extract_artifact_json(response)
    assert payload is not None and payload["attempts"] == 2
    assert GOOD_CODE in response
    assert FAIL_CODE not in response

    # 修复提示词拿到了错误原文与失败代码
    retry_user = _user_text(scripted.calls[3])
    assert "假渲染器注入的失败" in retry_user
    assert MOCK_FAIL_MARKER in retry_user
    assert RETRY_MARK in scripted.calls[3].messages[0]["content"]
    assert scripted.exhausted and len(scripted.calls) == 5


async def test_math_animator_gives_up_after_four_attempts(ws_client):
    """代码一直失败：总尝试 4 次（1 生成 + 3 修复）后收场——done 里给代码与日志，不是 error。"""
    scripted = _script(
        ANALYSIS,
        DESIGN,
        _fence(FAIL_CODE),
        _fence(FAIL_CODE),
        _fence(FAIL_CODE),
        _fence(FAIL_CODE),
        SUMMARY,
    )
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    attempts = [m for m in _status_messages(events) if "次尝试" in m]
    assert len(attempts) == 4
    assert "第 4/4 次尝试" in attempts[-1]

    assert events[-1]["type"] == "done"  # 渲染失败也不吃掉回合格
    response = events[-1]["payload"]["response"]
    assert extract_artifact_json(response) is None  # 没有产物围栏
    assert "连续 4 次渲染都没成功" in response
    assert FAIL_CODE in response  # 最后一版代码还在
    assert "[mock]" in response
    # 4 条渲染尝试 + 3 次修复 + 3 个前置阶段 + 1 次总结
    assert len(scripted.calls) == 7


async def test_math_animator_stops_when_repair_has_no_code(ws_client):
    """修复段没产出代码块：不再拿同一版白渲染，直接收场。"""
    scripted = _script(
        ANALYSIS, DESIGN, _fence(FAIL_CODE), "这次我解释一下为什么失败（但没有代码块）", SUMMARY
    )
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    attempts = [m for m in _status_messages(events) if "次尝试" in m]
    assert len(attempts) == 1  # 只渲染了第一次
    response = events[-1]["payload"]["response"]
    assert "连续 1 次渲染都没成功" in response
    assert len(scripted.calls) == 5


async def test_math_animator_no_scene_class_goes_to_repair(ws_client):
    """代码里没有场景类：本地判掉不进渲染进程，直接让模型修，修好第二轮成功。"""
    scripted = _script(ANALYSIS, DESIGN, _fence(NO_SCENE_CODE), _fence(GOOD_CODE), SUMMARY)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    response = events[-1]["payload"]["response"]
    payload = extract_artifact_json(response)
    assert payload is not None and payload["attempts"] == 2
    assert "没有找到可渲染的场景类" in _user_text(scripted.calls[3])
    assert NO_SCENE_CODE not in response


async def test_math_animator_missing_deps_friendly_error(ws_client_real_deps):
    """没装 manim/ffmpeg：友好 error + 安装指引，一次 LLM 调用都不烧。"""
    created = []

    def factory():
        client = _script(ANALYSIS)
        created.append(client)
        return client

    install_scripted(factory)
    with ws_client_real_deps.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    message = errors[-1]["payload"]["message"]
    assert "缺少数学动画依赖" in message
    assert "math-animator" in message and "ffmpeg" in message
    assert created == []
    assert not any(e["type"] == "status" for e in events)


async def test_math_animator_invalid_quality_fails_fast(ws_client):
    """非法 quality：报 error(recoverable=false)，0 次 LLM 调用。"""
    created = []

    def factory():
        client = _script(ANALYSIS)
        created.append(client)
        return client

    install_scripted(factory)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_animator(ws, config={"quality": "ultra"})
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    assert "ultra" in errors[-1]["payload"]["message"]
    assert created == []
