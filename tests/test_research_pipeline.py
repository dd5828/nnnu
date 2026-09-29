"""deep_research 两段式回合（§7.6；验收「每阶段进度可见 + 报告有真实来源」的脚本化金路径）。

一次调研跨两个用户回合，脚本 7 步对应 7 次 LLM 调用（多跑一步就抛「脚本耗尽」，
所以「哪一段该调几次」是天然校验）：

| 回合 | 步骤 |
|---|---|
| 1 | 澄清（1）→ 分解（1） |
| 2 | 子问题1：检索轮（1）+ 摘要（1）；子问题2：同上（2）→ 成稿（1） |

断言覆盖：两回合各自的 status 序列、每段各有各的系统提示、流式增量拼起来 == content_done
== done.response == 落库 assistant、cost_summary 每回合恰好一次、工具白名单按段收窄
（分解段与成稿段是空集——「恰好一次调用」靠这个保证）、`[CIT-…]` 重编号与参考资料、
状态机流转（confirming → researching → reported/partial）、子问题失败只降级成 warning、
非法档位零 LLM 打回、改稿走重分解、`mode=answer` 换提示词。
"""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from nnnu.api.main import create_app
from nnnu.capabilities.research.capability import STAGE_TOOLS, _SourceMarker
from nnnu.core.tool_protocol import ToolContext, ToolResult
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.research.models import DEPTH_SPECS
from nnnu.services.sessions.schema import db_path

TOPIC = "调研 2025 年 RAG 的主流方案"
REFINED = "研究主题：2025 年 RAG 的主流方案，范围限定在检索链路与工程取舍，面向要落地的工程团队。"
SUB_JSON = json.dumps(
    {
        "sub_topics": [
            {"title": "检索链路", "overview": "召回与重排的现状"},
            {"title": "工程取舍", "overview": "成本、延迟与选型"},
        ]
    },
    ensure_ascii=False,
)
SUB1_TEXT = "检索链路摘要：混合检索是主流 [CIT-1-1]，重排普遍多加一步 [CIT-1-2]。"
SUB2_TEXT = "工程取舍摘要：成本与延迟是主要矛盾 [CIT-2-1]，向量库选型差异不大 [CIT-2-2]。"
# 报告里故意留一个凭空编号的记号：必须被剥掉并计一次 warning
REPORT_TEXT = (
    "综合结论：混合检索加重排是 2025 年的主流 [CIT-1-1][CIT-1-2]，"
    "成本与延迟仍是主要权衡 [CIT-2-1]，另有一条对不上来源的说法 [CIT-9-9]。"
)
# 重排后应有的样子：CIT-1-1→[1]、CIT-1-2→[2]、CIT-2-1→[2]（跨子问题复用 B 的号）、
# CIT-9-9 剥成空串
REPORT_RENUMBERED = (
    "综合结论：混合检索加重排是 2025 年的主流 [1][2]，"
    "成本与延迟仍是主要权衡 [2]，另有一条对不上来源的说法 。"
)
SINGLE_TOPIC_TEXT = "我觉得这个主题不用拆，直接查吧。"

# 来源的形状照抄 web_search 的 detail["sources"]（kb=web 是它写死的）
A = {"doc_id": "https://example.com/a", "kb": "web", "title": "来源 A", "snippet": "A 的摘要"}
B = {"doc_id": "https://example.com/b", "kb": "web", "title": "来源 B", "snippet": "B 的摘要"}
C = {"doc_id": "https://example.com/c", "kb": "web", "title": "来源 C", "snippet": "C 的摘要"}
SEARCH_RESULTS = [[A, B], [B, C]]  # 第二次检索重复 B：跨子问题去重、复用同一个号

# 提示词 YAML（prompts/zh/deep_research.yaml）里的区分串：各段系统提示的身份标记
CLARIFY_MARK = "【澄清阶段】"
DECOMPOSE_MARK = "【分解阶段】"
RESEARCH_MARK = "【检索阶段】"
REPORT_MARK = "【报告阶段】"
ANSWER_MARK = "【作答阶段】"
OUTLINE_HEADING = "## 研究大纲"
REVISED_HEADING = "## 研究大纲（已按你的意见调整）"
REFERENCES_HEADING = "## 参考资料"

USAGE = {"prompt_tokens": 30, "completion_tokens": 12}


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


@pytest.fixture
def fake_search(ws_client, monkeypatch):
    """替掉 web_search 的 run：夹具里一个网络请求都不发，来源固定可断言。

    补在注册表单例上（`mount_tools` 每回合都从那儿取实例），所以两个回合都吃这一份。
    """
    from nnnu.runtime.registry.tool_registry import get_tool_registry

    tool = get_tool_registry().get("web_search")
    assert tool is not None, "web_search 未注册：装配或挂载开关变了"
    calls: list[dict] = []

    async def _run(ctx: ToolContext) -> ToolResult:
        calls.append(ctx.args)
        sources = SEARCH_RESULTS[min(len(calls) - 1, len(SEARCH_RESULTS) - 1)]
        return ToolResult(
            ok=True,
            output=f"检索到 {len(sources)} 条结果",
            detail={"sources": [dict(item) for item in sources]},
        )

    monkeypatch.setattr(tool, "run", _run)
    return calls


def _call(name: str, call_id: str, **args) -> LLMToolCall:
    return LLMToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))


def _research_steps() -> list[ScriptedStep]:
    """第二回合的 5 步：两个子问题各「检索一轮 + 收尾摘要」，最后成稿一次。"""
    return [
        ScriptedStep(
            tool_calls=[_call("web_search", "s1", query="RAG 混合检索 2025")],
            finish_reason="tool_calls",
            usage=USAGE,
        ),
        ScriptedStep(chunks=[SUB1_TEXT], usage=USAGE),
        ScriptedStep(
            tool_calls=[_call("web_search", "s2", query="RAG 成本 延迟 权衡")],
            finish_reason="tool_calls",
            usage=USAGE,
        ),
        ScriptedStep(chunks=[SUB2_TEXT], usage=USAGE),
        ScriptedStep(chunks=[REPORT_TEXT], usage=USAGE),
    ]


def _script(*, turn2: list[ScriptedStep] | None = None) -> ScriptedLLM:
    head = [
        ScriptedStep(chunks=[REFINED], usage=USAGE),
        ScriptedStep(chunks=[SUB_JSON], usage=USAGE),
    ]
    return ScriptedLLM([*head, *(_research_steps() if turn2 is None else turn2)])


def _send(ws, *, message, capability="deep_research", config=None, session_id="sess-research"):
    payload: dict = {
        "type": "chat",
        "session_id": session_id,
        "message": message,
        "capability": capability,
        "language": "zh",
    }
    if config is not None:
        payload["config"] = config
    ws.send_json(payload)


def _receive_until(ws, predicate, limit=300) -> list[dict]:
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"未等到目标事件，已收 {len(events)} 条")


def _turn(ws, **kwargs) -> list[dict]:
    _send(ws, **kwargs)
    return _receive_until(ws, lambda e: e["type"] in {"done", "error", "stopped"})


def _wait_turn_settled(ws_client, session_id: str) -> None:
    runtime = ws_client.app.state.runtime
    for _ in range(100):
        if runtime.active_turn_for(session_id) is None:
            return
        time.sleep(0.05)
    raise AssertionError("回合未在预期时间内收尾")


def _stages_of(events: list[dict]) -> list[str]:
    return [e["payload"]["stage"] for e in events if e["type"] == "status"]


def _of(events: list[dict], type_: str) -> list[dict]:
    return [e for e in events if e["type"] == type_]


def _streamed(events: list[dict]) -> str:
    return "".join(e["payload"]["text"] for e in _of(events, "content_delta"))


def _user_text(call) -> str:
    return next(m["content"] for m in reversed(call.messages) if m["role"] == "user")


def _tool_names(call) -> set[str]:
    return {schema["function"]["name"] for schema in call.tools}


def _rows(session_id: str) -> list[dict]:
    """直接读库（调研没有 REST 端点，§9.1 不加）：连接用完就关，不干扰应用的连接。

    库在 `<NNNU_HOME>/data/user/neolearn.db`（`get_data_root()` 又拼了一层 data）。
    Windows 上裸路径的 file: URI 认不出盘符，走 as_uri() 的正规形态（只读打开）。
    """
    from nnnu.runtime.home import get_data_root

    connection = sqlite3.connect(f"{db_path(get_data_root()).as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM research_runs WHERE session_id = ? ORDER BY created_at", (session_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def test_source_marker_numbers_and_titles():
    """记号生成器：按来源顺序连续编号、标题压平成一行、没有来源就不加尾巴。"""
    marker = _SourceMarker(2, lambda marks: f"【记号】\n{marks}")
    assert marker.annotate("原文", []) == "原文"  # 无来源：原样返回，不留空尾巴

    out = marker.annotate("原文", [A, {"doc_id": "https://example.com/x", "title": "多\n行  标题"}])
    assert out.startswith("原文\n\n【记号】\n")
    assert "[CIT-2-1] 来源 A" in out
    assert "[CIT-2-2] 多 行 标题" in out
    # 标题缺省时退回 doc_id：记号总得能被模型认出是哪一条
    assert "[CIT-2-3] https://example.com/y" in marker.annotate(
        "", [{"doc_id": "https://example.com/y"}]
    )
    # 记号是 CitationBook.add_subtopic 的唯一来源：两者必须同一份列表、同一顺序
    assert [item["doc_id"] for item in marker.sources] == [
        "https://example.com/a",
        "https://example.com/x",
        "https://example.com/y",
    ]


async def test_two_turn_golden_path(ws_client, fake_search):
    scripted = _script()
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        # ── 回合 1：澄清 → 分解 → 大纲（到此为止，等用户答复）──
        first = _turn(ws, message=TOPIC, config={"depth": "standard", "mode": "report"})

    assert first[-1]["type"] == "done"
    outline = first[-1]["payload"]["response"]
    assert _stages_of(first) == ["rephrasing", "decomposing"]
    assert all(e["payload"]["message"] for e in _of(first, "status"))

    # 大纲正文：标题 + 主题 + 两条子问题 + 确认提示
    assert OUTLINE_HEADING in outline
    assert TOPIC not in outline  # 正文用的是精炼后的主题
    assert REFINED in outline
    assert "1. **检索链路** — 召回与重排的现状" in outline
    assert "2. **工程取舍** — 成本、延迟与选型" in outline
    assert "回复「确认」" in outline
    # 澄清段与分解段的正文/JSON 都不该漏进用户可见的正文
    assert SUB_JSON not in outline
    assert _streamed(first) == outline
    assert _of(first, "content_done")[-1]["payload"]["full_text"] == outline

    # 段落系统提示各就各位（分解段不带检索段的提示）
    systems = [call.messages[0]["content"] for call in scripted.calls]
    assert CLARIFY_MARK in systems[0] and RESEARCH_MARK not in systems[0]
    assert DECOMPOSE_MARK in systems[1] and CLARIFY_MARK not in systems[1]
    # 工具白名单按段收窄：澄清段只有 ask_user，分解段是空集（保证恰好一次调用）
    assert _tool_names(scripted.calls[0]) == {"ask_user"}
    assert _tool_names(scripted.calls[1]) == set()
    # 分解段的任务里带上了精炼主题与档位要求的子问题数
    decompose_input = _user_text(scripted.calls[1])
    assert REFINED in decompose_input
    assert str(DEPTH_SPECS["standard"].subtopics) in decompose_input

    session_id = first[0]["session_id"]
    _wait_turn_settled(ws_client, session_id)
    rows = _rows(session_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "confirming"
    assert rows[0]["refined_topic"] == REFINED
    assert rows[0]["depth"] == "standard" and rows[0]["mode"] == "report"
    assert [item["title"] for item in json.loads(rows[0]["subtopics"])] == ["检索链路", "工程取舍"]

    # ── 回合 2：打字「确认」→ 检索 ×2 → 成稿 ──
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        second = _turn(ws, message="确认", session_id=session_id)

    assert second[-1]["type"] == "done"
    report = second[-1]["payload"]["response"]
    assert _stages_of(second) == ["researching", "researching", "researching", "reporting"]
    progress = [
        e["payload"]["message"]
        for e in _of(second, "status")
        if "正在检索" in e["payload"]["message"]
    ]
    assert any("1/2" in item and "检索链路" in item for item in progress)
    assert any("2/2" in item and "工程取舍" in item for item in progress)

    # 研究段正文：小标题 + 每个子问题一行证据小结；报告段接在后面
    assert "## 研究过程" in report
    assert "### 1. 检索链路" in report and "### 2. 工程取舍" in report
    assert report.count("- 检索到 2 条证据") == 2
    assert "## 研究报告" in report
    # 子问题摘要是素材，不作为正文照搬；报告正文只有成稿段写的那一份
    assert SUB1_TEXT not in report and SUB2_TEXT not in report

    # 引用重编号：子问题命名空间 → 全局号，凭空编号的记号被剥掉
    assert REPORT_RENUMBERED in report
    assert "[CIT-" not in report
    dropped = [e for e in _of(second, "warning") if "引用对不上" in e["payload"]["message"]]
    assert len(dropped) == 1 and "1 处" in dropped[0]["payload"]["message"]

    # 参考资料：代码生成、URL 可点、跨子问题去重、只列正文真引用过的
    assert REFERENCES_HEADING in report
    references = report.split(REFERENCES_HEADING)[1]
    assert "1. [来源 A](https://example.com/a)" in references
    assert "2. [来源 B](https://example.com/b)" in references
    # 来源 C 检索到了但正文一处都没引：不进清单（清单说的是「引用了什么」，
    # 不是「一共搜到什么」——不然搜索通道的噪音页会冒充成报告的来源）
    assert "https://example.com/c" not in report
    assert len([line for line in references.splitlines() if line.strip()]) == 2
    assert not _of(second, "error")
    assert _of(second, "content_done")[-1]["payload"]["full_text"] == report
    assert _streamed(second) == report

    # 每个子问题各跑一次循环：第二个子问题的上下文里没有第一个子问题的摘要
    scripts = scripted.calls[2:]
    for call in scripts[:4]:
        assert _tool_names(call) <= set(STAGE_TOOLS["researching"])
        assert "web_search" in _tool_names(call)
        assert RESEARCH_MARK in call.messages[0]["content"]
    assert SUB1_TEXT not in "".join(str(m.get("content")) for m in scripts[3].messages)
    assert "2/2" in _user_text(scripts[2])

    # 引用记号由系统递到模型手边：工具结果末尾附「记号 + 标题」，模型照抄，不自己数号
    # （旧写法要它在子问题内跨工具调用连续数 nn，数错就整段引用错位）
    tool_1 = next(m for m in scripts[1].messages if m["role"] == "tool")
    assert "[CIT-1-1] 来源 A" in tool_1["content"]
    assert "[CIT-1-2] 来源 B" in tool_1["content"]
    tool_2 = next(m for m in scripts[3].messages if m["role"] == "tool")
    assert "[CIT-2-1] 来源 B" in tool_2["content"]  # 子问题内的号：B 在这个子问题排第一
    assert "[CIT-2-2] 来源 C" in tool_2["content"]
    # 记号只进模型看到的那一份；工具卡照旧发原文，界面上不该多出引用记号
    assert all("[CIT-" not in e["payload"]["summary"] for e in _of(second, "tool_result"))

    # 成稿段：空工具集、报告系统提示、子问题摘要作为素材交进去
    report_call = scripts[4]
    assert _tool_names(report_call) == set()
    assert REPORT_MARK in report_call.messages[0]["content"]
    assert RESEARCH_MARK not in report_call.messages[0]["content"]
    report_input = _user_text(report_call)
    assert SUB1_TEXT in report_input and SUB2_TEXT in report_input

    # 来源走正文不走 done：参考资料在报告里，done 上不带 citations
    # （带了的话「引用来源」面板和会话导出会跟正文里的清单重复显示同一批来源）
    assert second[-1]["payload"]["citations"] == []
    assert fake_search and len(fake_search) == 2

    # 脚本 7 步全用尽：每段该调几次就是几次
    assert scripted.exhausted and len(scripted.calls) == 7
    # 成本汇总每回合恰一次
    assert len(_of(first, "cost_summary")) == 1 and len(_of(second, "cost_summary")) == 1

    # 落库：两段 assistant 各自与对应回合的正文逐字一致
    _wait_turn_settled(ws_client, session_id)
    detail = ws_client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["capability"] == "deep_research"
    users = [m for m in detail["messages"] if m["role"] == "user"]
    assistants = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert [m["content"] for m in users] == [TOPIC, "确认"]
    assert [m["content"] for m in assistants] == [outline, report]

    # 状态机走到底：confirming → researching → reported，答复锚在第二条用户消息上
    rows = _rows(session_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "reported"
    assert json.loads(rows[0]["failed_subtopics"]) == []
    assert rows[0]["answer_message_id"] == users[-1]["id"]


async def test_subtopic_failure_only_warns(ws_client, fake_search):
    """一个子问题连续产生无效工具调用：降级成 warning，回合照常 done，报告标 partial。"""
    bad = [
        ScriptedStep(
            tool_calls=[_call("no_such_tool", "b1")], finish_reason="tool_calls", usage=USAGE
        ),
        ScriptedStep(
            tool_calls=[_call("no_such_tool", "b2")], finish_reason="tool_calls", usage=USAGE
        ),
    ]
    tail = _research_steps()[2:]  # 第二个子问题 + 成稿照常
    scripted = _script(turn2=[*bad, *tail])
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _turn(ws, message=TOPIC)
        session_id = "sess-research"
        _wait_turn_settled(ws_client, session_id)
        second = _turn(ws, message="确认", session_id=session_id)

    assert second[-1]["type"] == "done"  # 子问题的 error 没有吃掉整个回合的信封
    assert not _of(second, "error")
    warnings = [e["payload"]["message"] for e in _of(second, "warning")]
    assert any("无效工具调用" in item for item in warnings)

    report = second[-1]["payload"]["response"]
    assert "- 本子问题未完成，未纳入报告" in report
    assert "其中 1 个未能完成（检索链路）" in report
    assert SUB1_TEXT not in report  # 失败的那个子问题没有素材
    assert SUB2_TEXT not in report

    _wait_turn_settled(ws_client, session_id)
    rows = _rows(session_id)
    assert rows[0]["status"] == "partial"
    assert json.loads(rows[0]["failed_subtopics"]) == ["检索链路"]


async def test_revision_redecomposes_in_place(ws_client, fake_search):
    """不是确认就是改稿：就地重跑一次分解、换掉大纲、直接开查（不再问第二次）。"""
    revised_json = json.dumps(
        {
            "sub_topics": [
                {"title": "评测方法", "overview": "怎么量指标"},
                {"title": "工程取舍", "overview": "成本、延迟与选型"},
            ]
        },
        ensure_ascii=False,
    )
    steps = [
        ScriptedStep(chunks=[revised_json], usage=USAGE),
        *_research_steps(),
    ]
    scripted = _script(turn2=steps)
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _turn(ws, message=TOPIC)
        session_id = "sess-research"
        _wait_turn_settled(ws_client, session_id)
        second = _turn(ws, message="第 1 条换成评测方法", session_id=session_id)

    assert second[-1]["type"] == "done"
    report = second[-1]["payload"]["response"]
    assert REVISED_HEADING in report
    assert "1. **评测方法**" in report
    # 第二回合的第 3 次调用就是重跑分解（改稿不走确认），第 4 次才进检索
    assert DECOMPOSE_MARK in scripted.calls[2].messages[0]["content"]
    assert "1. **检索链路**" in _user_text(scripted.calls[2])  # 旧大纲作为素材带进去
    assert RESEARCH_MARK in scripted.calls[3].messages[0]["content"]

    _wait_turn_settled(ws_client, session_id)
    rows = _rows(session_id)
    assert len(rows) == 1  # 改稿不新建行，会话绑定不丢
    assert rows[0]["status"] == "reported"
    assert [item["title"] for item in json.loads(rows[0]["subtopics"])] == ["评测方法", "工程取舍"]


async def test_confirm_action_beats_wording(ws_client, fake_search):
    """前端确认按钮：config 里带 research_action 就算数，不必指望用户打出确认词。"""
    scripted = _script()
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _turn(ws, message=TOPIC)
        session_id = "sess-research"
        _wait_turn_settled(ws_client, session_id)
        # 这句话不是确认词（剥完还剩「还」），只有按钮的强信号能把它判成确认
        second = _turn(
            ws,
            message="这个大纲看着还行",
            session_id=session_id,
            config={"research_action": "confirm"},
        )

    assert second[-1]["type"] == "done"
    assert DECOMPOSE_MARK not in scripted.calls[2].messages[0]["content"]  # 没有重跑分解
    assert RESEARCH_MARK in scripted.calls[2].messages[0]["content"]


async def test_answer_mode_swaps_report_prompt(ws_client, fake_search):
    """mode=answer：四阶段与两段式不变，只把成稿段换成「直接作答」的提示词。"""
    scripted = _script()
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _turn(ws, message=TOPIC, config={"depth": "quick", "mode": "answer"})
        session_id = "sess-research"
        _wait_turn_settled(ws_client, session_id)
        second = _turn(ws, message="确认", session_id=session_id)

    assert second[-1]["type"] == "done"
    report_call = scripted.calls[-1]
    assert ANSWER_MARK in report_call.messages[0]["content"]
    assert REPORT_MARK not in report_call.messages[0]["content"]


async def test_stale_researching_run_does_not_swallow_new_question(ws_client, fake_search):
    """上次跑一半断了（脚本耗尽 → 回合报 error，行留在 researching）：下一条消息是新问题，
    不该被当成对旧大纲的答复接着跑旧主题。"""
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[REFINED], usage=USAGE),
            ScriptedStep(chunks=[SUB_JSON], usage=USAGE),
            # 第二回合只给一步就没了：检索轮中途「断线」
            ScriptedStep(
                tool_calls=[_call("web_search", "s1", query="RAG")],
                finish_reason="tool_calls",
                usage=USAGE,
            ),
        ]
    )
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        _turn(ws, message=TOPIC)
        session_id = "sess-research"
        _wait_turn_settled(ws_client, session_id)
        broken = _turn(ws, message="确认", session_id=session_id)
        assert broken[-1]["type"] == "error"
        _wait_turn_settled(ws_client, session_id)
        rows = _rows(session_id)
        assert rows[0]["status"] == "researching"  # 卡在中间态

    # 换一份脚本：新问题应当从澄清重新开始（而不是接着旧大纲查）
    fresh = _script()
    install_scripted(lambda: fresh)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        third = _turn(ws, message="换个题目：调研向量数据库选型", session_id=session_id)

    assert third[-1]["type"] == "done"
    assert _stages_of(third) == ["rephrasing", "decomposing"]
    rows = _rows(session_id)
    assert len(rows) == 2
    assert rows[0]["status"] == "abandoned"  # 旧行作废，不占部分唯一索引
    assert rows[1]["status"] == "confirming"
    assert rows[1]["topic"] == "换个题目：调研向量数据库选型"


async def test_invalid_params_fail_fast(ws_client):
    created = []

    def factory():
        client = _script()
        created.append(client)
        return client

    install_scripted(factory)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        events = _turn(ws, message=TOPIC, config={"depth": "extreme", "mode": "report"})

    errors = _of(events, "error")
    assert len(errors) == 1
    assert errors[-1]["payload"]["recoverable"] is False
    assert "extreme" in errors[-1]["payload"]["message"]
    assert not any(e["type"] == "done" for e in events)
    assert not _of(events, "status")
    assert created == []  # 校验在校验层就结束，没建客户端也没调模型


async def test_broken_outline_falls_back_to_single_subtopic(ws_client, fake_search):
    """分解段没守 JSON 协议：warning + 整题当一个子问题，回合不空转。"""
    scripted = ScriptedLLM(
        [
            ScriptedStep(chunks=[REFINED], usage=USAGE),
            ScriptedStep(chunks=[SINGLE_TOPIC_TEXT], usage=USAGE),
        ]
    )
    install_scripted(lambda: scripted)
    with ws_client.websocket_connect("/api/v1/ws") as ws:
        first = _turn(ws, message=TOPIC)

    assert first[-1]["type"] == "done"
    assert any("大纲" in e["payload"]["message"] for e in _of(first, "warning"))
    outline = first[-1]["payload"]["response"]
    assert SINGLE_TOPIC_TEXT not in outline
    assert f"1. **{REFINED}**" in outline  # 整题兜底成一个子问题
    assert scripted.exhausted

    session_id = first[0]["session_id"]
    _wait_turn_settled(ws_client, session_id)
    rows = _rows(session_id)
    assert [item["title"] for item in json.loads(rows[0]["subtopics"])] == [REFINED]
