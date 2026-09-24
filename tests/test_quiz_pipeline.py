"""deep_question 两阶段流水线（§7.4；验收①②的脚本化金路径）。

脚本三步 = 构思（流式）+ 生成（流式但正文吞掉）+ 自评/修正（非流式）——
多跑一轮就抛「脚本耗尽」，所以「每回合恰好 3 次 LLM 调用」是天然校验。断言覆盖：
1. status 事件序列恰好 ideation → generation（前端步骤条的依据）；
2. 构思段的流式增量是最终正文的前缀；正文含渲染后的题目（题面 + 选项 + LaTeX）；
3. 入库的行数与正文、config 的 session_id 一致，选项/答案/解析与正文对得上；
4. 与库里重复的题被丢弃，正文里给出跳过提示（§7.4 验收「重复率 < 20%」）；
5. content_done 恰好两次（构思段一次 + 能力收尾一次），最后那次 == done.response
   == 落库正文；cost_summary 恰好一次；
6. 坏 num_questions → error(recoverable=false)、零 LLM 调用；
7. 生成段不挂工具、构思段的工具在白名单内。
"""

import time

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.capabilities.question.capability import STAGE_TOOLS
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

QUESTION = "出 5 道关于导数与链式法则的题"
PLAN_TEXT = "构思：考点是导数的定义、幂函数求导与链式法则；单选题考定义，解答题考链式法则。"
SESSION_ID = "sess-quiz"

# 生成段与审校段的产出（模型视角的 JSON 原文：注意 $\frac 是单反斜杠——
# JSON 会把它当 \f 转义吃掉，正好验证宽松修复那一层）
GENERATED_JSON = r"""```json
[
  {"type": "single", "stem": "函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？",
   "options": ["2", "4", "8", "16"], "answer": "B",
   "explanation": "先求导得 $2x$，代入 $x=2$ 得 4。", "knowledge_point": "导数", "difficulty": "easy"},
  {"type": "single", "stem": "曲线 $y=\frac{1}{2}x^2$ 在 $x=1$ 处的切线斜率是多少？",
   "options": ["$1$", "$2$", "$\frac{1}{2}$", "$0$"], "answer": "A",
   "explanation": "求导得 $x$，代入 $x=1$ 得斜率 1。", "knowledge_point": "导数", "difficulty": "medium"},
  {"type": "multi", "stem": "下列哪些函数在 $x=0$ 处可导？",
   "options": ["$x^2$", "$\sin x$", "$|x|$", "$x^3$"], "answer": "ABD",
   "explanation": "$|x|$ 在 $x=0$ 处左右导数不相等，不可导。", "knowledge_point": "可导性", "difficulty": "medium"},
  {"type": "single", "stem": "求 $\sin(2x)$ 的导数时，外层函数是什么？",
   "options": ["$\sin u$", "$2x$", "$\cos u$", "$2$"], "answer": "A",
   "explanation": "链式法则先对外层 $\sin u$ 求导，再乘内层导数 2。", "knowledge_point": "链式法则", "difficulty": "easy"},
  {"type": "short", "stem": "写出链式法则的求导公式，并说明它什么时候用。",
   "options": [], "answer": "复合函数求导时，外层导数乘以内层导数。",
   "explanation": "链式法则处理复合结构，逐层求导再相乘。", "knowledge_point": "链式法则", "difficulty": "hard"}
]
```"""

# 审校段：题目与顺序都不变，只把第 3 题的答案解析补全（审校就该只改必须改的）
REVIEWED_JSON = GENERATED_JSON.replace("不可导。", "不可导（左右导数不相等）。")


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


def _quiz_script() -> ScriptedLLM:
    usage = {"prompt_tokens": 20, "completion_tokens": 9}
    return ScriptedLLM(
        [
            ScriptedStep(chunks=[PLAN_TEXT], usage=usage),
            ScriptedStep(chunks=[GENERATED_JSON], usage=usage),
            ScriptedStep(chunks=[REVIEWED_JSON], usage=usage),
        ]
    )


def _run_quiz(ws, *, message=QUESTION, config=None, session_id=SESSION_ID) -> None:
    payload: dict = {
        "type": "chat",
        "session_id": session_id,
        "message": message,
        "capability": "deep_question",
        "language": "zh",
    }
    if config is not None:
        payload["config"] = config
    ws.send_json(payload)


def _receive_until(ws, predicate, limit=400) -> list[dict]:
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"未等到目标事件，已收 {len(events)} 条")


def _wait_turn_settled(ws_client, session_id: str) -> None:
    runtime = ws_client.app.state.runtime
    for _ in range(100):
        if runtime.active_turn_for(session_id) is None:
            return
        time.sleep(0.05)
    raise AssertionError("回合未在预期时间内收尾")


def _stages_of(events: list[dict]) -> list[str]:
    return [e["payload"]["stage"] for e in events if e["type"] == "status"]


def _questions_of(ws_client) -> list[dict]:
    response = ws_client.get("/api/v1/questions")
    assert response.status_code == 200
    return response.json()["questions"]


async def test_quiz_golden_path(ws_client, repo_prompts):
    scripted = _quiz_script()
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_quiz(ws)
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    # ① 两阶段按序点亮，且每段带文案
    assert events[-1]["type"] == "done", events[-1]
    assert _stages_of(events) == ["ideation", "generation"]
    assert all(e["payload"]["message"] for e in events if e["type"] == "status")

    response = events[-1]["payload"]["response"]
    # ② 构思段是流式的（用户看得到），生成段是静默的（模型那段产出是 JSON）
    streamed = "".join(e["payload"]["text"] for e in events if e["type"] == "content_delta")
    assert streamed and response.startswith(streamed)
    assert PLAN_TEXT in streamed
    assert "```json" not in response  # 原始 JSON 绝不进正文
    assert response.count(PLAN_TEXT) == 1

    # ② 续：题目被渲染成 markdown（题号 + 题型 + 选项标签），答案不外露
    for index in range(1, 6):
        assert f"### 第 {index} 题" in response
    assert "- A. " in response and "- D. " in response
    assert "链式法则" in response
    # LaTeX 从 JSON 里活着出来了（$\frac 的单反斜杠没被转义吃掉）
    assert r"$\frac{1}{2}$" in response
    assert r"$\sin(2x)$" in response
    assert "先求导得" not in response  # 解析不进正文

    # ③ 入库与正文一致
    rows = _questions_of(ws_client)
    assert len(rows) == 5
    assert {row["source"] for row in rows} == {"deep_question"}
    assert {row["session_id"] for row in rows} == {SESSION_ID}
    by_stem = {row["stem"]: row for row in rows}
    tangent = by_stem[r"曲线 $y=\frac{1}{2}x^2$ 在 $x=1$ 处的切线斜率是多少？"]
    assert tangent["answer"] == "A"
    assert tangent["options"] == [r"$1$", r"$2$", r"$\frac{1}{2}$", r"$0$"]
    assert tangent["type"] == "single"
    assert tangent["difficulty"] == "medium"
    # 审校版的解析是入库的那份（审校结果优先）
    multi = by_stem["下列哪些函数在 $x=0$ 处可导？"]
    assert multi["answer"] == "ABD" and multi["type"] == "multi"
    assert "左右导数不相等" in multi["explanation"]
    # 正文里的题干与库里的题面逐字一致（看到的 == 存下的，§7.3 的教训）
    for row in rows:
        assert row["stem"] in response

    # ④ content_done 两次：构思段收尾一次、能力收尾一次；最后那次就是落库正文
    content_done = [e for e in events if e["type"] == "content_done"]
    assert len(content_done) == 2
    assert content_done[-1]["payload"]["full_text"] == response
    assert sum(1 for e in events if e["type"] == "cost_summary") == 1
    assert sum(1 for e in events if e["type"] == "tool_call") == 0  # 生成段没挂工具，也没被调用

    # ⑤ 提示词就位：构思段带考点规划任务，生成段带出题规格；工具白名单按段收窄
    systems = [call.messages[0]["content"] for call in scripted.calls]
    assert "构思阶段" in systems[0]
    assert "生成阶段" in systems[1] and "出题规格" in systems[1]
    assert "审校" in systems[2]
    tool_names = {schema["function"]["name"] for schema in scripted.calls[0].tools}
    assert tool_names <= set(STAGE_TOOLS["ideation"])
    assert not scripted.calls[1].tools  # 生成段不挂工具
    assert not scripted.calls[2].tools
    # 接力：生成段的用户消息里带着构思段的产出
    generation_user = next(
        m["content"] for m in reversed(scripted.calls[1].messages) if m["role"] == "user"
    )
    assert PLAN_TEXT in generation_user

    # ⑥ 每回合恰好三次调用，脚本一步不剩
    assert scripted.exhausted
    assert len(scripted.calls) == 3

    # ⑦ 落库的 assistant 消息与 done.response 逐字一致；能力粘在会话上
    _wait_turn_settled(ws_client, SESSION_ID)
    detail = ws_client.get(f"/api/v1/sessions/{SESSION_ID}").json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[-1]["content"] == response
    assert detail["capability"] == "deep_question"


async def test_quiz_drops_duplicates(ws_client, repo_prompts):
    """生成的第一题与库里已有的一模一样 → 丢弃，其余照常入库（§7.4 重复率验收）。"""
    # 先往库里放一道「上回生成的」同题（题面与选项都一样）
    stored = await ws_client.app.state.questions.create_question(
        stem="函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？",
        options=["2", "4", "8", "16"],
        answer="B",
        explanation="先求导得 $2x$，代入 $x=2$ 得 4。",
        type="single",
        knowledge_point="导数",
    )
    assert stored.id.startswith("q-")

    scripted = _quiz_script()
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_quiz(ws, session_id="sess-quiz-dup")
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error"})

    assert events[-1]["type"] == "done", events[-1]
    response = events[-1]["payload"]["response"]
    assert "重复" in response  # 正文里给出跳过提示
    rows = _questions_of(ws_client)
    assert len(rows) == 5  # 库里原本 1 道 + 本轮入库 4 道
    assert [row["id"] for row in rows].count(stored.id) == 1
    assert sum(1 for row in rows if row["session_id"] == "sess-quiz-dup") == 4
    assert scripted.exhausted and len(scripted.calls) == 3


async def test_bad_config_fails_fast(ws_client):
    """题量越界：报 error(recoverable=false) 收尾，不建客户端、不烧 LLM 调用。"""
    created = []

    def factory():
        client = _quiz_script()
        created.append(client)
        return client

    install_scripted(factory)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _run_quiz(ws, config={"num_questions": 99})
        events = _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    assert "99" in errors[-1]["payload"]["message"]
    assert not any(e["type"] == "done" for e in events)
    assert created == []
    assert not any(e["type"] == "status" for e in events)
    assert _questions_of(ws_client) == []
