"""mastery_path 脚本化金路径（§7.5；§14「为线性代数基础生成路径并完成 2 个节点的循环」）。

一场从头到尾的戏：**4 个回合 / 19 次 LLM 调用**（脚本多一步少一步都会当场炸：
少一步 = 回合没跑完就"脚本耗尽"，多一步 = 结尾 `len(scripted.calls)` 对不上）。

**记账规则**（改脚本前必须懂）：`ScriptedLLM` 按调用序消费步骤，所以
① 循环里每一轮 = 1 次调用；② **本能力已无次级调用**——出题权归模型后，题目由模型在
循环里自己写（`mastery_quiz` 只是登记），定性门也由模型自己判（`mastery_assess`），
没有补题调用、也没有独立评定器；③ **卡片阻塞本身 0 次调用**：等用户作答不花钱；
④ 每道题的往返是 2 轮——「登记 + 发卡」同轮（工具顺序执行：`mastery_quiz` 登记完，
`ask_user` 才发卡）、「判分」一轮。

盯的是**三段式答题闭环**（拍板 #1）：模型 `mastery_quiz` 登记题目与答案（答案留服务端）
→ 模型自己调 `ask_user` 发卡 → 用户在同一个 WS 上 `ask_user_reply` 作答 → 模型
`mastery_grade` 判分、写回掌握度。全程没有 advance（拍板 #2）：回合 3 里三道题
**都不点名节点**也照样落在同一个节点上——下一目标是服务端现算的。

另外守四件事：
- 四类两套门（拍板 #3）：concept/design 走 `mastery_assess`、procedure 走题到 90 分；
  头两道全对也只到 80（置信封顶：至少 3 次作答才可能过门，见 learning/mastery.py）；
- **卡面归位**：模型在 `ask_user` 里瞎写的题面/选项一律不作数，服务端按库里那道题重发；
- **模型转述无效**：判分只认 `ask_user` 缝落库的学习者原话，`mastery_grade` 的 `answer`
  参数在有原话时被忽略（脚本里故意塞一个错答案来证这件事）；
- **看到的 == 存下的**：流出去的正文字节 == 落库的助手消息（`_TurnTranscriptBus` 的职责）。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep

SESSION = "sess-learn"
GHOST = "lpath-ghost"
TOPIC = "线性代数基础"

# 四类里的三类：概念（定性）/ 操作（定量 90）/ 设计（定性）——四类两套门各走一遍
NODES = [
    {"title": "向量与线性组合", "type": "concept", "description": "先弄清线性组合是什么"},
    {"title": "矩阵与行列式", "type": "procedure", "parent_index": 0},
    {"title": "特征值分解", "type": "design", "parent_index": 1},
]

# 三道单选题（模型现场出的，答案键只留在服务端）；正确项故意不都在 B 上，
# 顺带证明「标签 ↔ 选项」的映射是真在算的
Q1 = {
    "question": "二阶矩阵 [[1,2],[3,4]] 的行列式是多少？",
    "question_type": "single",
    "options": ["-2", "-4", "2", "10"],
    "expected_answer": "A",
    "explanation": "ad − bc = 1×4 − 2×3 = −2。",
    "difficulty": "easy",
}
Q2 = {
    "question": "把矩阵的第一行乘以 3，行列式会怎么变？",
    "question_type": "single",
    "options": ["不变", "变成 3 倍", "变成 1/3", "变成 9 倍"],
    "expected_answer": "B",
    "explanation": "某一行乘 k，行列式也乘 k。",
    "difficulty": "medium",
}
Q3 = {
    "question": "上三角矩阵的行列式等于什么？",
    "question_type": "single",
    "options": ["主对角线之和", "主对角线之积", "0", "1"],
    "expected_answer": "B",
    "explanation": "上三角矩阵的行列式是主对角线元素之积。",
    "difficulty": "medium",
}


@pytest.fixture
def ws_client(tmp_home, repo_prompts):
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


def _call(name: str, call_id: str, **args) -> LLMToolCall:
    return LLMToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))


def _script() -> ScriptedLLM:
    usage = {"prompt_tokens": 30, "completion_tokens": 12}
    return ScriptedLLM(
        [
            # ── 回合 1（建路径）：先 mastery_status（还没绑路径，它会把库里已有的列出来）→
            #     没有对得上的才 mastery_build → 收尾 ──
            ScriptedStep(
                tool_calls=[_call("mastery_status", "c1")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call(
                        "mastery_build",
                        "c2",
                        topic=TOPIC,
                        title=TOPIC,
                        summary="从向量到特征值",
                        nodes=NODES,
                    )
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=["路径建好了，我们就从第一个节点开始。"], usage=usage),
            # ── 回合 2（节点1 概念 → 定性门）：status → 讲解 + ask_user 请人讲一遍 →
            #     模型自己判 mastery_assess → 收尾（全程 0 次次级调用）──
            ScriptedStep(
                chunks=["线性组合就是「加法」与「数乘」这两件事：把向量拼起来描述同一个空间。"],
                tool_calls=[_call("mastery_status", "c3")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call(
                        "ask_user",
                        "c4",
                        question="请用自己的话把「向量与线性组合」讲一遍：它是什么、为什么成立、什么时候用得上。",
                        options=[],
                        context="节点《向量与线性组合》",
                    )
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call(
                        "mastery_assess",
                        "c5",
                        passed=True,
                        feedback="定义、直觉与适用边界都讲到了。",
                    )
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=["讲清楚了，这个节点算过关。"], usage=usage),
            # ── 回合 3（节点2 操作 → 定量门 90）：三道题，每题「登记+发卡」一轮、「判分」一轮。
            #     头两道全对只到 80（2 次封顶），第三道才到 100 —— 门要两次以上证据 ──
            ScriptedStep(
                chunks=["行列式是线性变换对面积（体积）的缩放倍数。"],
                tool_calls=[_call("mastery_status", "c6")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            # 第一张卡：ask_user 的参数是**故意瞎写的**——服务端会按库里那道题归位
            ScriptedStep(
                tool_calls=[
                    _call("mastery_quiz", "c7", **Q1),
                    _call(
                        "ask_user",
                        "c8",
                        question="【模型自己编的题面，不该出现在卡上】",
                        options=[{"label": "Z", "description": "【编的选项】"}],
                        context="【编的小字】",
                    ),
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[_call("mastery_grade", "c9")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call("mastery_quiz", "c10", **Q2),
                    _call(
                        "ask_user",
                        "c11",
                        question="【编的题面】",
                        options=[],
                    ),
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[_call("mastery_grade", "c12")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call("mastery_quiz", "c13", **Q3),
                    _call(
                        "ask_user",
                        "c14",
                        question="【编的题面】",
                        options=[],
                    ),
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            # 这一笔判分故意传了个错答案（学习者在卡上敲的是 B）：服务端落库的原话优先，忽略它
            ScriptedStep(
                tool_calls=[_call("mastery_grade", "c15", answer="A")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=["三道都对了，这个节点过了 90 分的门。"], usage=usage),
            # ── 回合 4（节点3 设计 → 定性门）：status → ask → assess → 收尾 ──
            ScriptedStep(
                chunks=["特征值分解要解决的问题是：换一组基，让线性变换只表现为缩放。"],
                tool_calls=[_call("mastery_status", "c16")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call(
                        "ask_user",
                        "c17",
                        question="请用自己的话讲讲「特征值分解」要解决什么问题。",
                        options=[],
                        context="节点《特征值分解》",
                    )
                ],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[_call("mastery_assess", "c18", passed=True)],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                chunks=["整条路径的节点都过了，接下来可以加新节点或换主题。"], usage=usage
            ),
        ]
    )


def _send(
    ws, *, message: str, capability: str = "mastery_path", config: dict | None = None
) -> None:
    payload: dict = {
        "type": "chat",
        "session_id": SESSION,
        "message": message,
        "capability": capability,
        "language": "zh",
    }
    if config is not None:
        payload["config"] = config
    ws.send_json(payload)


# 终局事件（core/events.py:TERMINAL_TYPES）：error 也算——能力报错后不再补 done
TERMINAL = {"done", "error", "stopped"}


def _drain(ws, *, answers: tuple[str, ...] = (), limit: int = 800, trailing: int = 0) -> list[dict]:
    """收事件直到终局；遇到 `ask_user` 就按 `answers` 顺序回一条。

    **回复必须在读事件的过程中发**：卡片是阻塞的（工具在等答复，事件流到 ask_user 就
    停在那儿），`ask_user_reply` 走的是同一个 WS（§7.20 任意连接可投）。

    `trailing` 用于报错回合：error 之后编排器还会补一条 cost_summary，读掉它才不会
    污染下一回合的计数。
    """
    pending = list(answers)
    events: list[dict] = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "ask_user":
            ws.send_json(
                {
                    "type": "ask_user_reply",
                    "turn_id": event["turn_id"],
                    "ask_id": event["payload"]["ask_id"],
                    "answer": pending.pop(0) if pending else "",
                }
            )
        if event["type"] in TERMINAL:
            break
    else:
        raise AssertionError(f"未等到终局事件，已收 {len(events)} 条")
    for _ in range(trailing):
        events.append(ws.receive_json())
    return events


def _settle(ws_client) -> None:
    """回合真的收尾（消息落库、成本落库做完）才让下一个动作进场。"""
    runtime = ws_client.app.state.runtime
    for _ in range(500):
        if runtime.active_turn_for(SESSION) is None:
            return
        time.sleep(0.02)
    raise AssertionError("回合未在预期时间内收尾")


def _run(
    ws, ws_client, *, answers: tuple[str, ...] = (), trailing: int = 0, **kwargs
) -> list[dict]:
    _send(ws, **kwargs)
    events = _drain(ws, answers=answers, trailing=trailing)
    _settle(ws_client)
    return events


def _events_of(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def _signatures(events: list[dict]) -> list[str]:
    """工具调用序（工具名）——比裸计数更能定位漂移在哪一步。"""
    return [event["payload"]["tool_name"] for event in _events_of(events, "tool_call")]


def _response_of(events: list[dict]) -> str:
    done = _events_of(events, "done")
    assert done, [event["type"] for event in events]
    return done[-1]["payload"]["response"]


def _streamed_text(events: list[dict]) -> str:
    return "".join(event["payload"]["text"] for event in _events_of(events, "content_delta"))


def _asks(events: list[dict]) -> list[dict]:
    return [event["payload"] for event in _events_of(events, "ask_user")]


def _results(events: list[dict]) -> list[dict]:
    return [event["payload"] for event in _events_of(events, "tool_result")]


def _detail(ws_client, path_id: str) -> dict:
    response = ws_client.get(f"/api/v1/learning/paths/{path_id}")
    assert response.status_code == 200
    return response.json()


def _node_of(detail: dict, node_id: str) -> dict:
    return next(node for node in detail["nodes"] if node["id"] == node_id)


def _questions_of(ws_client, node_id: str) -> list[dict]:
    response = ws_client.get("/api/v1/questions", params={"node_id": node_id})
    assert response.status_code == 200
    return response.json()["questions"]


async def test_mastery_golden_path(ws_client):
    scripted = _script()
    install_scripted(lambda: scripted)  # 必须同一实例：步序即全局调用序
    sessions = ws_client.app.state.runtime._sessions
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        # ---- 负例：坏 path_id / 坏配置 → 一条不可恢复 error，且一次模型都没打 ----
        ghost = _run(ws, ws_client, message="继续学习", config={"path_id": GHOST}, trailing=1)
        errors = _events_of(ghost, "error")
        assert len(errors) == 1 and errors[0]["payload"]["recoverable"] is False
        assert GHOST in errors[0]["payload"]["message"]
        assert ghost[-1]["type"] == "cost_summary"  # error 是终局事件，信封只补成本

        bad_config = _run(
            ws,
            ws_client,
            message="继续学习",
            config={"path_id": GHOST, "practice_count": 99},
            trailing=1,
        )
        errors = _events_of(bad_config, "error")
        assert len(errors) == 1 and "补题数量 99" in errors[0]["payload"]["message"]
        assert scripted.calls == [], "配置或路径就错的回合不该打模型"

        # ---- 回合 1：先 mastery_status（库里空的）→ mastery_build 建树 ----
        created = _run(ws, ws_client, message="我要学线性代数基础")
        assert _signatures(created) == ["mastery_status", "mastery_build"]
        # 还没建树不是「调用出错」：模型接着 build 就得，所以 ok=True
        assert _results(created)[0]["ok"] is True
        assert len(_events_of(created, "cost_summary")) == 1
        assert _response_of(created) == "路径建好了，我们就从第一个节点开始。"

        cards = ws_client.get("/api/v1/learning/paths").json()["paths"]
        assert len(cards) == 1
        path_id = cards[0]["id"]
        # 列表卡片给的是「下一目标」（没有 current_node_id 这一列了）
        assert cards[0]["next_title"] == "向量与线性组合" and cards[0]["next_action"] == "assess"
        assert "current_node_id" not in cards[0]
        detail = _detail(ws_client, path_id)
        node1, node2, node3 = detail["nodes"]
        assert [(node["title"], node["depth"]) for node in detail["nodes"]] == [
            ("向量与线性组合", 0),
            ("矩阵与行列式", 1),
            ("特征值分解", 2),
        ]
        # 四类两套门：定性节点的门是 null（布尔门），定量节点是 90 分
        assert [node["node_type"] for node in detail["nodes"]] == [
            "concept",
            "procedure",
            "design",
        ]
        assert [node["gate_kind"] for node in detail["nodes"]] == [
            "qualitative",
            "quantitative",
            "qualitative",
        ]
        assert [node["gate"] for node in detail["nodes"]] == [None, 90.0, None]
        assert detail["stats"]["not_started"] == 3 and detail["stats"]["mastered"] == 0
        assert detail["path"]["session_id"] == SESSION  # 聊天建的路径顺手绑了会话
        assert detail["next_target"]["action"] == "assess"  # 概念节点先讲一遍

        # ---- 回合 2：概念节点。讲解 + assess（纯文本卡）→ 当场评定 → 过门 ----
        first = _run(
            ws, ws_client, message="开始学吧", answers=("线性组合就是把向量按标量加起来……",)
        )
        assert _signatures(first) == ["mastery_status", "ask_user", "mastery_assess"]
        asks = _asks(first)
        assert len(asks) == 1
        # 定性卡：没有选项、允许自由作答，题面是「请用自己的话讲一遍」
        assert asks[0]["options"] == [] and asks[0]["allow_free_text"] is True
        assert "向量与线性组合" in asks[0]["question"]
        # 用户的答复经 ask_user_reply 回到工具里（事件里也能看到回执）
        replies = _events_of(first, "ask_user_reply")
        assert len(replies) == 1 and replies[0]["payload"]["answer"].startswith("线性组合")
        assessed = _results(first)[-1]["detail"]
        assert assessed["cleared"] is True and assessed["mastery"] == 100.0
        # 定性门由**模型自己判**，没有独立评定器的那次调用（旧版这里是 "assessor"）
        assert assessed["cards"][0]["grading"]["source"] == "model"

        detail = _detail(ws_client, path_id)
        assert _node_of(detail, node1["id"])["assess_passed"] is True
        assert _node_of(detail, node1["id"])["state"] == "mastered"
        assert detail["stats"]["mastered"] == 1 and detail["stats"]["progress"] == round(1 / 3, 4)
        assert detail["next_target"]["node_id"] == node2["id"]
        assert detail["next_target"]["action"] == "probe"  # 没碰过的定量节点先摸底

        # ---- 回合 3：操作节点。三道题都不点名节点（下一目标服务端现算），
        #      头两道只到 80 没过门，第三道才 100 —— 门要两次以上证据 ----
        second = _run(ws, ws_client, message="继续", answers=("A", "B", "B"))
        assert _signatures(second) == [
            "mastery_status",
            "mastery_quiz",
            "ask_user",
            "mastery_grade",
            "mastery_quiz",
            "ask_user",
            "mastery_grade",
            "mastery_quiz",
            "ask_user",
            "mastery_grade",
        ]
        # 判分结果在 tool_result 里是第 3 / 6 / 9 条（每条签名各配一条 result）
        first_grade = _results(second)[3]["detail"]
        second_grade = _results(second)[6]["detail"]
        third_grade = _results(second)[9]["detail"]
        # 一次作答封顶 50 → 两次封顶 80 → 三次不封顶 100：门要两次以上证据
        assert first_grade["mastery"] == 50.0 and first_grade["cleared"] is False
        assert first_grade["cards"][0]["grading"]["correct"] is True
        assert second_grade["mastery"] == 80.0 and second_grade["cleared"] is False
        assert third_grade["mastery"] == 100.0 and third_grade["cleared"] is True

        asks = _asks(second)
        assert len(asks) == 3
        # **卡面归位**：脚本里 ask_user 传的是「【模型自己编的题面…】」，卡片上必须是库里那道题
        assert [ask["question"] for ask in asks] == [Q1["question"], Q2["question"], Q3["question"]]
        assert [ask["options"] for ask in asks] == [
            [
                {"label": label, "description": text}
                for label, text in zip("ABCD", question["options"])
            ]
            for question in (Q1, Q2, Q3)
        ]
        assert "编" not in json.dumps(asks, ensure_ascii=False)
        assert all(ask["allow_free_text"] is True for ask in asks)
        assert {ask["context"] for ask in asks} == {"节点《矩阵与行列式》"}
        # 出卡时不给答案键与解析（判分之后才允许出现）
        assert all("answer" not in ask and "explanation" not in ask for ask in asks)

        # **模型转述无效**：第 3 题的判分脚本传了 answer="A"（错的），
        # 学习者在卡上敲的是 B —— 服务端落库的原话优先，判出来必须是「对」
        assert third_grade["cards"][0]["answer"] == "B"
        assert third_grade["cards"][0]["question"]["answer"] == "B"  # 这一处是判完之后
        # 第一题的正确项是 A（不是默认的 B），标签映射确实在算
        assert first_grade["cards"][0]["answer"] == "A"

        questions = _questions_of(ws_client, node2["id"])
        assert len(questions) == 3  # 三道都是模型现场出的
        assert {question["source"] for question in questions} == {"mastery"}
        assert all(question["node_id"] == node2["id"] for question in questions)
        assert sorted(question["stem"] for question in questions) == sorted(
            [Q1["question"], Q2["question"], Q3["question"]]
        )
        attempts = [
            attempt
            for question in questions
            for attempt in (await ws_client.app.state.questions.list_attempts(question["id"]))
        ]
        assert len(attempts) == 3 and all(attempt.correct for attempt in attempts)

        detail = _detail(ws_client, path_id)
        node = _node_of(detail, node2["id"])
        assert node["mastery"] == 100.0 and node["cleared"] is True
        assert node["state"] == "mastered" and node["review_stage"] >= 1
        assert node["next_review_at"] > time.time()
        assert detail["next_target"]["node_id"] == node3["id"]

        # ---- 回合 4：设计节点。assess 不点名 → 服务端给的下一目标还是它 ----
        third = _run(ws, ws_client, message="最后这一节呢", answers=("换一组基以后只看缩放倍数……",))
        assert _signatures(third) == ["mastery_status", "ask_user", "mastery_assess"]
        assess_payload = _results(third)[-1]["detail"]
        assert assess_payload["cards"][0]["answer"].startswith("换一组基")  # 原话进库了
        assert assess_payload["cleared"] is True

        detail = _detail(ws_client, path_id)
        assert detail["stats"] == {
            "total": 3,
            "mastered": 3,
            "learning": 0,
            "not_started": 0,
            "due": 0,
            "progress": 1.0,
            "weak": 0,
        }
        assert detail["weak_points"] == []
        assert detail["next_target"]["action"] == "complete"  # 全过门了

        # 交互日志：5 张卡（3 道题 + 2 次定性评定）全部落定，没有一张挂着
        rows = await ws_client.app.state.db.fetch_all(
            "SELECT kind, status, grade_source FROM learning_interactions ORDER BY created_at"
        )
        assert len(rows) == 5
        assert all(row["status"] == "graded" for row in rows)
        assert [row["kind"] for row in rows].count("assess") == 2
        # 两道定性门都是模型自判（旧版的 assessor 已退役）
        assert [row["grade_source"] for row in rows if row["kind"] == "assess"] == [
            "model",
            "model",
        ]

        # ---- 「看到的 == 存下的」：三段正文流出去多少，库里就存多少 ----
        assistant = [
            message.content
            for message in await sessions.list_messages(SESSION)
            if message.role == "assistant"
        ]
        assert len(assistant) == 4
        assert assistant[0] == _response_of(created)
        assert assistant[1] == _response_of(first)
        assert assistant[2] == _response_of(second)  # 带工具调用的那一轮也不许丢正文
        assert assistant[3] == _response_of(third)
        assert _streamed_text(second) == _response_of(second)

        # ---- 判分只认落库的原话：库里存的是学习者在卡上敲的那份，不是模型转述的 ----
        # （出题权归模型后，答案键本来就在模型上下文里，没什么可拦的；真正要守的是这一列——
        #   第 3 题的判分脚本塞了 answer="A"，库里仍然必须是学习者敲的 B）
        stored = await ws_client.app.state.db.fetch_all(
            "SELECT user_answer FROM learning_interactions WHERE kind = 'quiz' ORDER BY created_at"
        )
        assert [row["user_answer"] for row in stored] == ["A", "B", "B"]

    # 19 步一步不剩：每个回合的调用次数都在账上（记账规则见模块头）
    assert len(scripted.calls) == 19
