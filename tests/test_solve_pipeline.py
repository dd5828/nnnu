"""deep_solve 三阶段流水线（§7.3；验收②的脚本化金路径）。

脚本三步 = 三段各一次 LLM 调用：多跑一轮脚本就耗尽抛错，所以「每阶段恰好一轮」
是天然校验，不用另外数数。断言覆盖：
1. status 事件序列恰好 planning → reasoning → writing（前端步骤条的依据）；
2. 正文含三段小标题；流式增量拼起来 == 最终合并文本（界面只变长、不回跳）；
3. done.response == 最后一个 content_done == 落库的 assistant 消息（看到的和存的一致）；
4. cost_summary 恰好一次；
5. 每段各有各的系统提示与工具白名单，接力文本带上了上一段的产出；
6. mode=hint 换成 system_hint（只给引导问题链）；非法 mode 直接报错、不烧 LLM 调用。
"""

import time

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.capabilities.solve.capability import STAGE_TOOLS
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

QUESTION = "求 d/dx[sin(x²)]"
PLAN_TEXT = "规划产物：考点是链式法则，令 u=x²。"
DERIVE_TEXT = "推导产物：d/dx[sin(u)]=cos(u)·2x，代回 u=x²。"
ANSWER_MARK = "结论是 2x·cos(x²)"
ANSWER_TEXT = f"教学级解答：{ANSWER_MARK}。"
HINT_TEXT = "提示问题链：先想想外层函数的导数是什么？"

# 提示词 YAML（prompts/{zh,en}/deep_solve.yaml）里的区分串：各段系统提示的身份标记
PLAN_MARK = "【规划阶段】"
REASON_MARK = "【推理阶段】"
WRITE_MARK = "【书写阶段】"
HINT_MARK = "引导问题链"  # 只出现在 stages.writing.system_hint
STAGE_KEYS = ("planning", "reasoning", "writing")


@pytest.fixture
def ws_client(tmp_home):
    """TestClient 跑 lifespan（数据库/运行时装配在 portal 线程事件循环）。"""
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


def _three_stage_script(*, mode="full") -> ScriptedLLM:
    usage = {"prompt_tokens": 12, "completion_tokens": 7}
    last = HINT_TEXT if mode == "hint" else ANSWER_TEXT
    return ScriptedLLM(
        [
            ScriptedStep(chunks=[PLAN_TEXT], usage=usage),
            ScriptedStep(chunks=[DERIVE_TEXT], usage=usage),
            ScriptedStep(chunks=[last], usage=usage),
        ]
    )


def _run_solve(ws, *, message=QUESTION, config=None, session_id="sess-solve") -> None:
    payload: dict = {
        "type": "chat",
        "session_id": session_id,
        "message": message,
        "capability": "deep_solve",
        "language": "zh",
    }
    if config is not None:
        payload["config"] = config
    ws.send_json(payload)


def _wait_turn_settled(ws_client, session_id: str) -> None:
    """done 先发、落库在收尾时完成：不等它，后面读会话会读到半截。"""
    runtime = ws_client.app.state.runtime
    for _ in range(100):
        if runtime.active_turn_for(session_id) is None:
            return
        time.sleep(0.05)
    raise AssertionError("回合未在预期时间内收尾")


def _stages_of(events: list[dict]) -> list[str]:
    return [e["payload"]["stage"] for e in events if e["type"] == "status"]


async def test_three_stage_golden_path(ws_client, repo_prompts):
    scripted = _three_stage_script()
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_solve(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    # ① 三段按序点亮，且每段带文案（步骤条的 label）
    assert _stages_of(events) == list(STAGE_KEYS)
    assert all(e["payload"]["message"] for e in events if e["type"] == "status")

    # ② 三段小标题都在正文里（标题即提示词 YAML 的 heading，与界面看到的一致）
    assert events[-1]["type"] == "done"
    response = events[-1]["payload"]["response"]
    for heading in ("解题规划", "详细推导", "教学级解答"):
        assert f"## {heading}" in response
    assert response.count(PLAN_TEXT) == 1  # 只在规划段出现一次
    assert response.count(DERIVE_TEXT) == 1
    assert response.count(ANSWER_TEXT) == 1
    assert response.index(PLAN_TEXT) < response.index(DERIVE_TEXT) < response.index(ANSWER_TEXT)

    # ② 续：流式增量 == 最终合并文本（前端按全文覆盖，界面不会回跳）
    streamed = "".join(e["payload"]["text"] for e in events if e["type"] == "content_delta")
    assert streamed == response
    content_done = [e for e in events if e["type"] == "content_done"]
    assert content_done and content_done[-1]["payload"]["full_text"] == response

    # 三段各一轮：脚本三步全用尽（多跑一轮会抛「脚本耗尽」）
    assert scripted.exhausted
    assert len(scripted.calls) == 3

    # ④ 成本汇总恰好一次
    assert sum(1 for e in events if e["type"] == "cost_summary") == 1

    # ⑤ 每段的系统提示各就各位：规划段不带推导段的提示，反之亦然
    systems = [call.messages[0]["content"] for call in scripted.calls]
    assert PLAN_MARK in systems[0] and REASON_MARK not in systems[0]
    assert REASON_MARK in systems[1] and PLAN_MARK not in systems[1]
    assert WRITE_MARK in systems[2]
    # 接力：第二段/第三段的用户消息里带着上一段的产出
    assert _user_text(scripted.calls[1]).count(PLAN_TEXT) == 1
    assert _user_text(scripted.calls[2]).count(DERIVE_TEXT) == 1
    # 首段只有「本阶段任务」，没有「上一阶段产出」
    assert "本阶段任务" in _user_text(scripted.calls[0])
    assert PLAN_TEXT not in _user_text(scripted.calls[0])

    # ⑤ 工具白名单按段收窄（默认挂载集里的 web_search/cron 不该漏进推导、书写段）
    assert _tool_names(scripted.calls[0]) <= set(STAGE_TOOLS["planning"])
    assert _tool_names(scripted.calls[0]) >= {"web_search"}
    assert _tool_names(scripted.calls[1]) <= set(STAGE_TOOLS["reasoning"])
    assert _tool_names(scripted.calls[1]) >= {"reason"}
    assert _tool_names(scripted.calls[2]) <= set(STAGE_TOOLS["writing"])
    assert "web_search" not in _tool_names(scripted.calls[1])
    assert "reason" not in _tool_names(scripted.calls[2])

    # 落库的 assistant 消息与 done.response 逐字一致
    session_id = events[0]["session_id"]
    _wait_turn_settled(ws_client, session_id)
    detail = ws_client.get(f"/api/v1/sessions/{session_id}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[-1]["content"] == response
    # 能力粘在会话上（后续回合/重新生成跟着走）
    assert detail["capability"] == "deep_solve"


def _user_text(call) -> str:
    return next(m["content"] for m in reversed(call.messages) if m["role"] == "user")


def _tool_names(call) -> set[str]:
    return {schema["function"]["name"] for schema in call.tools}


async def test_hint_mode_uses_hint_system(ws_client, repo_prompts):
    """hint 模式：书写段换 system_hint（只给引导问题链），输出里没有答案。"""
    scripted = _three_stage_script(mode="hint")
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_solve(ws, config={"mode": "hint"})
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    assert _stages_of(events) == list(STAGE_KEYS)
    response = events[-1]["payload"]["response"]
    assert HINT_TEXT in response
    assert ANSWER_MARK not in response
    writing_system = scripted.calls[2].messages[0]["content"]
    assert HINT_MARK in writing_system
    assert "整合成一份教学级解答" not in writing_system  # 完整解答段的提示词没被用上
    # 前两段不受 mode 影响
    assert PLAN_MARK in scripted.calls[0].messages[0]["content"]


async def test_invalid_mode_fails_fast(ws_client):
    """非法 mode：报 error(recoverable=false) 收尾，不烧一次 LLM 调用。"""
    created = []

    def factory():
        client = _three_stage_script()
        created.append(client)
        return client

    install_scripted(factory)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_solve(ws, config={"mode": "essay"})
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    assert "essay" in errors[-1]["payload"]["message"]
    assert not any(e["type"] == "done" for e in events)
    assert created == []  # 校验在校验层就结束了，没建客户端也没调模型
    assert not any(e["type"] == "status" for e in events)
