"""mastery_path 脚本化金路径（§7.5 重做版；§14「为线性代数基础生成路径并完成 2 个节点的循环」）。

一场从头到尾的戏：**4 个回合 / 14 次 LLM 调用**（脚本多一步少一步都会当场炸：
少一步 = 回合没跑完就"脚本耗尽"，多一步 = 结尾 `len(scripted.calls)` 对不上）。

**记账规则**（改脚本前必须懂）：`ScriptedLLM` 按调用序消费步骤，所以
① 循环里每一轮 = 1 次调用；② **工具内的次级调用也占位**——缺题时的补题、`assess` 的
定性评定都是真 LLM 调用（各 1 次，位置固定在「发起它的那次工具往返之后」）；
③ **卡片阻塞本身 0 次调用**：等用户作答不花钱；答复回来后客观题是确定性判分（0 次），
只有简答题才可能落到判分器。金路径全是单选，所以 6 张卡一共只多出 2 次补题 + 2 次评定。

盯的是**回合内答题闭环**（拍板 #1：答题搬进聊天）：服务端从题库渲染卡片 → 用户在同一个
WS 上 `ask_user_reply` 作答 → 工具当场判分、写回掌握度 → 判分结果进下一轮消息历史，
模型这一轮就看得见。全程没有 advance（拍板 #2）：回合 3 里 `probe` 摸完没过门，
紧接着的 `quiz` **不点名节点**也照样落在同一个节点上——下一目标是服务端现算的。

另外守四件事：
- 四类两套门 + probe（拍板 #3）：concept/design 走 `assess`、procedure 走题到 90 分；
  摸底两道只到 80（至少 3 次作答才可能过门，见 mastery.py）；
- 卡片形状：题面/选项由服务端渲染（模型不写题），定性卡是纯文本卡（无选项、可自由作答）；
- **答案不外泄**：未作答题的解析一次都没进过任何一次请求（题干与选项该给，解析与答案键不给）；
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

# 出题契约见 prompts/zh/mastery.yaml: quiz.task；答案统一取 B，卡片一律回 "B"，
# 于是「第几张卡」与断言无关（选题顺序是「未作答的、后建的先来」）
PROBE_ROWS = json.dumps(
    {
        "questions": [
            {
                "type": "single",
                "stem": "二阶矩阵 [[1,2],[3,4]] 的行列式是多少？",
                "options": ["-2", "-4", "2", "10"],
                "answer": "B",
                "explanation": "ad − bc = 1×4 − 2×3 = −2。",
                "difficulty": "easy",
            },
            {
                "type": "single",
                "stem": "把矩阵的第一行乘以 3，行列式会怎么变？",
                "options": ["不变", "变成 3 倍", "变成 1/3", "变成 9 倍"],
                "answer": "B",
                "explanation": "某一行乘 k，行列式也乘 k。",
                "difficulty": "medium",
            },
        ]
    },
    ensure_ascii=False,
)
EXTRA_ROWS = json.dumps(
    {
        "questions": [
            {
                "type": "single",
                "stem": "上三角矩阵的行列式等于什么？",
                "options": ["主对角线之和", "主对角线之积", "0", "1"],
                "answer": "B",
                "explanation": "上三角矩阵的行列式是主对角线元素之积。",
                "difficulty": "medium",
            },
            {
                "type": "single",
                "stem": "矩阵可逆的充要条件是行列式满足什么？",
                "options": ["行列式等于 1", "行列式不为 0", "行列式大于 0", "行列式为整数"],
                "answer": "B",
                "explanation": "行列式非零 ⇔ 矩阵可逆。",
                "difficulty": "hard",
            },
        ]
    },
    ensure_ascii=False,
)
ASSESS_PASS = json.dumps(
    {"passed": True, "feedback": "定义、直觉与适用边界都讲到了。"}, ensure_ascii=False
)
# 不该出现在任何一次请求里的字符串（判分之后才允许出现的是「参考答案：B」这种标签，不是解析）
ANSWERS_AND_EXPLANATIONS = [
    "ad − bc = 1×4 − 2×3 = −2。",
    "某一行乘 k，行列式也乘 k。",
    "上三角矩阵的行列式是主对角线元素之积。",
    "行列式非零 ⇔ 矩阵可逆。",
]


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
            # ── 回合 1（建路径）：先 paths 看库里有没有 → 没有才 build → 收尾（0 次次级调用）──
            ScriptedStep(
                tool_calls=[_call("mastery", "c1", action="paths")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(
                tool_calls=[
                    _call(
                        "mastery",
                        "c2",
                        action="build",
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
            # ── 回合 2（节点1 概念 → 定性门）：讲解 + assess（卡里等用户讲一遍）→
            #     定性评定（1 次次级调用）→ 收尾 ──
            ScriptedStep(
                chunks=["线性组合就是「加法」与「数乘」这两件事：把向量拼起来描述同一个空间。"],
                tool_calls=[_call("mastery", "c3", action="assess")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=[ASSESS_PASS], usage=usage),
            ScriptedStep(chunks=["讲清楚了，这个节点算过关。"], usage=usage),
            # ── 回合 3（节点2 操作 → 定量门 90）：讲解 + probe 摸两道（没到门）→
            #     补题 1 次 → 同回合再 quiz 出两道（补题 1 次）→ 收尾 ──
            ScriptedStep(
                chunks=["行列式是线性变换对面积（体积）的缩放倍数。"],
                tool_calls=[_call("mastery", "c4", action="probe")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=[PROBE_ROWS], usage=usage),
            ScriptedStep(
                tool_calls=[_call("mastery", "c5", action="quiz", author=4, cards=2)],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=[EXTRA_ROWS], usage=usage),
            ScriptedStep(chunks=["四道都对了，这个节点过了 90 分的门。"], usage=usage),
            # ── 回合 4（节点3 设计 → 定性门）：讲解 + assess（不点名节点，走服务端的下一目标）→
            #     定性评定 → 收尾 ──
            ScriptedStep(
                chunks=["特征值分解要解决的问题是：换一组基，让线性变换只表现为缩放。"],
                tool_calls=[_call("mastery", "c6", action="assess")],
                finish_reason="tool_calls",
                usage=usage,
            ),
            ScriptedStep(chunks=[ASSESS_PASS], usage=usage),
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
    """工具调用签名表（`name:action`）——比裸计数更能定位漂移在哪一步。"""
    return [
        f"{event['payload']['tool_name']}:{event['payload']['args'].get('action')}"
        for event in _events_of(events, "tool_call")
    ]


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

        # ---- 回合 1：先 paths（库里空的）→ build 建树 ----
        created = _run(ws, ws_client, message="我要学线性代数基础")
        assert _signatures(created) == ["mastery:paths", "mastery:build"]
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
        assert _signatures(first) == ["mastery:assess"]
        asks = _asks(first)
        assert len(asks) == 1
        # 定性卡：没有选项、允许自由作答，题面是「请用自己的话讲一遍」
        assert asks[0]["options"] == [] and asks[0]["allow_free_text"] is True
        assert "向量与线性组合" in asks[0]["question"]
        # 用户的答复经 ask_user_reply 回到工具里（事件里也能看到回执）
        replies = _events_of(first, "ask_user_reply")
        assert len(replies) == 1 and replies[0]["payload"]["answer"].startswith("线性组合")
        assessed = _results(first)[0]["detail"]
        assert assessed["cleared"] is True and assessed["mastery"] == 100.0
        assert assessed["cards"][0]["grading"]["source"] == "assessor"

        detail = _detail(ws_client, path_id)
        assert _node_of(detail, node1["id"])["assess_passed"] is True
        assert _node_of(detail, node1["id"])["state"] == "mastered"
        assert detail["stats"]["mastered"] == 1 and detail["stats"]["progress"] == round(1 / 3, 4)
        assert detail["next_target"]["node_id"] == node2["id"]
        assert detail["next_target"]["action"] == "probe"  # 没碰过的定量节点先摸底

        # ---- 回合 3：操作节点。probe 两道 → 没过门 → 同一回合再 quiz 两道 → 过门 ----
        second = _run(ws, ws_client, message="继续", answers=("B", "B", "B", "B"))
        # 两个动作都**不点名节点**：第二张卡的目标是服务端在 probe 之后重算的
        assert _signatures(second) == ["mastery:probe", "mastery:quiz"]
        cards_payload = _results(second)[0]["detail"]
        assert len(cards_payload["cards"]) == 2
        # 摸底两道全对也只到 80（置信封顶），所以没过门——这是「门要两次以上证据」的设计
        assert cards_payload["mastery"] == 80.0 and cards_payload["cleared"] is False
        assert [card["grading"]["correct"] for card in cards_payload["cards"]] == [True, True]
        # 补题是真的发生了（每张卡都从题库里来，题面与选项由服务端渲染）
        asks = _asks(second)
        assert len(asks) == 4
        assert all(ask["options"] and ask["allow_free_text"] is True for ask in asks)
        assert {ask["context"] for ask in asks} == {
            "节点《矩阵与行列式》· 第 1/2 题",
            "节点《矩阵与行列式》· 第 2/2 题",
        }
        # 判分之后才给答案键：出卡时 payload 里没有 answer/explanation
        assert all("answer" not in ask and "explanation" not in ask for ask in asks)

        quiz_payload = _results(second)[1]["detail"]
        assert quiz_payload["mastery"] == 100.0 and quiz_payload["cleared"] is True
        assert quiz_payload["cards"][0]["question"]["answer"] == "B"  # 这一处是判完之后

        questions = _questions_of(ws_client, node2["id"])
        assert len(questions) == 4  # 摸底 2 道 + 练习补 2 道
        assert {question["source"] for question in questions} == {"mastery"}
        assert all(question["node_id"] == node2["id"] for question in questions)
        attempts = [
            attempt
            for question in questions
            for attempt in (await ws_client.app.state.questions.list_attempts(question["id"]))
        ]
        assert len(attempts) == 4 and all(attempt.correct for attempt in attempts)

        detail = _detail(ws_client, path_id)
        node = _node_of(detail, node2["id"])
        assert node["mastery"] == 100.0 and node["cleared"] is True
        assert node["state"] == "mastered" and node["review_stage"] >= 1
        assert node["next_review_at"] > time.time()
        assert detail["next_target"]["node_id"] == node3["id"]

        # ---- 回合 4：设计节点。assess 不点名 → 服务端给的下一目标还是它 ----
        third = _run(ws, ws_client, message="最后这一节呢", answers=("换一组基以后只看缩放倍数……",))
        assert _signatures(third) == ["mastery:assess"]
        assert _asks(third)[0]["context"] == "节点《特征值分解》· 用自己的话讲一遍"
        assert _results(third)[0]["detail"]["cleared"] is True

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

        # 交互日志：6 张卡（2 摸底 + 2 练习 + 2 评定）全部落定，没有一张挂着
        rows = await ws_client.app.state.db.fetch_all(
            "SELECT kind, status, grade_source FROM learning_interactions ORDER BY created_at"
        )
        assert len(rows) == 6
        assert all(row["status"] == "graded" for row in rows)
        assert [row["kind"] for row in rows].count("assess") == 2

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

        # ---- 答案不外泄：未作答题的解析一次都没进过任何一次请求 ----
        dumped = [json.dumps(request.messages, ensure_ascii=False) for request in scripted.calls]
        for probe in ANSWERS_AND_EXPLANATIONS:
            assert not any(probe in text for text in dumped), probe

    # 14 步一步不剩：每个回合的调用次数都在账上（记账规则见模块头）
    assert len(scripted.calls) == 14
