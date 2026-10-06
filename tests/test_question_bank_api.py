"""题库 REST（§9.1 + 判分端点 + P9 题库增强）：CRUD、筛选计数、作答判分、笔记/分类/变式题。

判分覆盖两条路：客观题确定性判分（不打模型），简答没把握时走 LLM 判分器
（脚本化替身），用量按合成 turn_id 写 usage_records。
P9 段：笔记 PATCH 往返（含时间戳语义）、错因筛选、相似题排序、AI 分类写回与
usage 记账、变式题预览（ai 与知识库 grounded 两条分支、预览不落库）、批量采纳。

用自带 app 句柄的 client（照 test_model_selection 的 client_app）：好几处断言
要绕过 API 直接读库（作答表、级联删除、成本表）。
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.errors import LLMConfigError
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.question_bank.service import RECENCY_WEIGHTS
from nnnu.services.rag.base import Hit

SINGLE = {
    "stem": "函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？",
    "type": "single",
    "options": ["2", "4", "8", "16"],
    "answer": "B",
    "explanation": "先求导得 $2x$，代入 $x=2$ 得 4。",
    "knowledge_point": "导数",
    "difficulty": "easy",
}

SHORT = {
    "stem": "简述光合作用中光反应发生的场所。",
    "type": "short",
    "options": [],
    "answer": "叶绿体的类囊体薄膜上",
    "explanation": "光反应需要色素与光系统，都在类囊体薄膜上。",
    "knowledge_point": "光合作用",
}


@pytest.fixture
async def bank(tmp_home):
    """(client, app)：题库用例多半要直接读库，所以连 app 一起给。"""
    from nnnu.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, app


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


async def _create(client, payload: dict) -> dict:
    response = await client.post("/api/v1/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _attempt(client, question_id: str, answer: str, **kwargs) -> dict:
    response = await client.post(
        f"/api/v1/questions/{question_id}/attempt", json={"answer": answer, **kwargs}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_question_crud_round_trip(bank):
    client, _app = bank
    created = await _create(client, SINGLE)
    assert created["id"].startswith("q-")
    assert created["source"] == "manual"
    assert created["options"] == SINGLE["options"]
    assert created["mastery"] == 0.0 and created["wrong_count"] == 0

    listed = (await client.get("/api/v1/questions")).json()
    assert [item["id"] for item in listed["questions"]] == [created["id"]]
    assert listed["counts"] == {"all": 1, "wrong": 0, "unanswered": 1}

    # 单题查询（P9 引用链路加）：引用深链与 ?question= 置顶都用它
    fetched = (await client.get(f"/api/v1/questions/{created['id']}")).json()
    assert fetched["stem"] == SINGLE["stem"]
    assert fetched["answer"] == "B"
    assert (await client.get("/api/v1/questions/q-nope")).status_code == 404

    patched = await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={"stem": "改过的题面：函数 $f(x)=x^2$ 在 $x=3$ 处的导数？", "answer": "C"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["answer"] == "C"
    # 只改答案时选项原样保留（局部更新是合并后整体复验）
    assert patched.json()["options"] == SINGLE["options"]

    assert (await client.delete(f"/api/v1/questions/{created['id']}")).json() == {
        "deleted": created["id"]
    }
    assert (await client.patch("/api/v1/questions/q-nope", json={"answer": "A"})).status_code == 404
    assert (await client.delete(f"/api/v1/questions/{created['id']}")).status_code == 404
    assert (await client.get("/api/v1/questions")).json()["counts"]["all"] == 0


async def test_patch_can_change_type(bank):
    client, _app = bank
    created = await _create(client, {**SINGLE, "answer": "BD", "type": "multi"})
    assert created["type"] == "multi"
    patched = await client.patch(
        f"/api/v1/questions/{created['id']}", json={"type": "single", "answer": "B"}
    )
    assert patched.json()["type"] == "single"


@pytest.mark.parametrize(
    "payload",
    [
        {**SINGLE, "stem": "  "},
        {**SINGLE, "answer": "E"},  # 标签越界
        {**SINGLE, "answer": "AB"},  # 单选给了两个
        {**SINGLE, "options": ["4"]},  # 选项太少
        {**SHORT, "options": ["A", "B"]},  # 简答带选项
        {**SHORT, "answer": ""},  # 简答没参考答案
    ],
)
async def test_create_rejects_invalid(bank, payload):
    client, _app = bank
    response = await client.post("/api/v1/questions", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_question"


async def test_list_filters_and_counts(bank):
    client, _app = bank
    clean = await _create(client, SINGLE)
    wrong = await _create(
        client, {**SINGLE, "stem": r"另一道导数题：$\sin x$ 的导数是什么？", "answer": "A"}
    )
    await _create(client, SHORT)

    await _attempt(client, clean["id"], "B")  # 答对：既不算错题，也不再是未作答
    await _attempt(client, wrong["id"], "D")  # 答错：进错题

    all_list = (await client.get("/api/v1/questions")).json()
    assert all_list["counts"] == {"all": 3, "wrong": 1, "unanswered": 1}

    wrong_list = (await client.get("/api/v1/questions", params={"filter": "wrong"})).json()
    assert [item["id"] for item in wrong_list["questions"]] == [wrong["id"]]
    assert wrong_list["questions"][0]["wrong_count"] == 1

    unanswered = (await client.get("/api/v1/questions", params={"filter": "unanswered"})).json()
    assert [item["knowledge_point"] for item in unanswered["questions"]] == ["光合作用"]

    by_point = (await client.get("/api/v1/questions", params={"knowledge_point": "导数"})).json()
    assert len(by_point["questions"]) == 2

    searched = (await client.get("/api/v1/questions", params={"search": "导"})).json()
    assert len(searched["questions"]) == 2

    assert (await client.get("/api/v1/questions", params={"filter": "none"})).status_code == 422


async def test_attempt_deterministic_grading_and_mastery(bank):
    client, _app = bank
    created = await _create(client, SINGLE)

    wrong = await _attempt(client, created["id"], "a")  # 小写标签也认
    assert wrong["grading"]["correct"] is False
    assert wrong["grading"]["source"] == "deterministic"
    assert wrong["grading"]["feedback"]
    assert wrong["question"]["wrong_count"] == 1
    assert wrong["question"]["mastery"] == 0.0
    assert wrong["question"]["last_attempt_at"] is not None
    assert wrong["attempt"]["answer"] == "a"  # 作答原文照样存下来

    right = await _attempt(client, created["id"], "B")
    assert right["grading"]["correct"] is True
    # 两次作答：0 分与 1 分按尾部权重加权再归一化，不能是「答对一次只有 0.5」
    expected = (0 * RECENCY_WEIGHTS[-2] + 1 * RECENCY_WEIGHTS[-1]) / sum(RECENCY_WEIGHTS[-2:])
    assert right["question"]["mastery"] == pytest.approx(round(expected, 4))
    assert right["question"]["wrong_count"] == 1  # 错题计数只加不减
    assert wrong["attempt"]["id"].startswith("qa-")


async def test_attempt_partial_score_and_invalid_answer(bank):
    client, _app = bank
    created = await _create(
        client,
        {
            **SINGLE,
            "type": "multi",
            "answer": "AC",
            "stem": "下列哪些是偶函数？",
            "options": ["$x^2$", "$x^3$", "$\\cos x$", "$\\sin x$"],
        },
    )
    partial = await _attempt(client, created["id"], "A")
    assert partial["grading"]["score"] == pytest.approx(0.5)
    assert partial["question"]["mastery"] == pytest.approx(0.5)

    # 标签越界（只有 A–D）→ 422 信封，作答不落库
    bad = await client.post(f"/api/v1/questions/{created['id']}/attempt", json={"answer": "AE"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_answer"

    missing = await client.post("/api/v1/questions/q-nope/attempt", json={"answer": "A"})
    assert missing.status_code == 404


async def test_attempt_short_answer_uses_llm_grader(bank, repo_prompts):
    client, app = bank
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=['{"score": 1, "correct": true, "feedback": "说到了类囊体薄膜，对。"}'],
                usage={"prompt_tokens": 40, "completion_tokens": 12},
            )
        ]
    )
    install_scripted(lambda: scripted)
    created = await _create(client, SHORT)

    body = await _attempt(client, created["id"], "在叶绿体里面", language="zh")
    assert body["grading"]["source"] == "llm"
    assert body["grading"]["correct"] is True
    assert body["grading"]["feedback"] == "说到了类囊体薄膜，对。"
    assert body["question"]["mastery"] == 1.0
    assert scripted.exhausted

    # 判分成本落进 usage_records（非回合通路：turn_id = grade-<attempt_id>）
    rows = await app.state.db.fetch_all(
        "SELECT session_id, turn_id, input_tokens, output_tokens FROM usage_records"
    )
    assert len(rows) == 1
    assert rows[0]["turn_id"] == f"grade-{body['attempt']['id']}"
    assert rows[0]["input_tokens"] == 40
    assert rows[0]["session_id"] == ""


async def test_attempt_short_answer_exact_match_skips_llm(bank):
    client, _app = bank
    created = await _create(client, SHORT)
    body = await _attempt(client, created["id"], "叶绿体的类囊体薄膜上")
    assert body["grading"]["source"] == "deterministic"
    assert body["grading"]["correct"] is True


async def test_delete_question_cascades_attempts(bank):
    client, app = bank
    created = await _create(client, SINGLE)
    await _attempt(client, created["id"], "B")
    rows = await app.state.db.fetch_all("SELECT id FROM question_attempts")
    assert len(rows) == 1

    assert (await client.delete(f"/api/v1/questions/{created['id']}")).status_code == 200
    assert await app.state.db.fetch_all("SELECT id FROM question_attempts") == []


CLASSIFY_REPLY = (
    '{"knowledge_point": "幂函数求导", "tags": ["易错"], '
    '"error_causes": ["概念不清"], "reason": "笔记提到公式记混"}'
)

VARIANTS_REPLY = json.dumps(
    {
        "variants": [
            {
                "stem": "函数 $g(x)=x^3$ 在 $x=2$ 处的导数是多少？",
                "options": ["6", "12", "8", "4"],
                "answer": "12",  # 给选项原文：唯一命中时归一成标签，不算猜
                "explanation": "先求导得 $3x^2$，代入得 12。",
                "difficulty": "easy",
            },
            # 坏草稿：答案读不准 → 整道丢弃（不猜答案键）
            {"stem": "坏题：答案不知所云", "options": ["6", "12"], "answer": "我不知道"},
        ]
    },
    ensure_ascii=False,
)


async def test_patch_note_round_trip_and_error_causes(bank):
    client, _app = bank
    created = await _create(client, SINGLE)
    assert created["note"] == "" and created["note_updated_at"] is None
    assert created["error_causes"] == []

    first = (
        await client.patch(
            f"/api/v1/questions/{created['id']}",
            json={"note": "第一次做错了：导数公式记混。\n\n（重做一遍）"},
        )
    ).json()
    assert first["note"].startswith("第一次做错了")
    assert "\n" in first["note"]  # 内部换行保留（Markdown 分段靠它）
    assert first["note_updated_at"] is not None

    # 改别的字段不动笔记时间（「改过别的字段」不该让笔记时间跳）
    touched = (
        await client.patch(f"/api/v1/questions/{created['id']}", json={"difficulty": "hard"})
    ).json()
    assert touched["note_updated_at"] == first["note_updated_at"]
    assert touched["note"] == first["note"]

    # 清空笔记：时间戳一并清掉（没有笔记就没有「笔记时间」）
    cleared = (await client.patch(f"/api/v1/questions/{created['id']}", json={"note": ""})).json()
    assert cleared["note"] == "" and cleared["note_updated_at"] is None

    # 错因白名单键往返
    causes = (
        await client.patch(
            f"/api/v1/questions/{created['id']}",
            json={"error_causes": ["calculation", "misread"]},
        )
    ).json()
    assert causes["error_causes"] == ["calculation", "misread"]

    # 未知错因 / 超上限 / 超长笔记都是 422，且不改库
    bad = await client.patch(f"/api/v1/questions/{created['id']}", json={"error_causes": ["随便"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_question"
    too_many = await client.patch(
        f"/api/v1/questions/{created['id']}",
        json={"error_causes": ["calculation", "misread", "concept_unclear", "method_missing"]},
    )
    assert too_many.status_code == 422
    long_note = await client.patch(f"/api/v1/questions/{created['id']}", json={"note": "x" * 4001})
    assert long_note.status_code == 422
    still = (await client.get("/api/v1/questions")).json()["questions"][0]
    assert still["error_causes"] == ["calculation", "misread"]


async def test_list_filters_by_error_cause(bank):
    client, _app = bank
    # 建题时就能带错因（表单通路），PATCH 也行（卡片通路）
    calc = await _create(client, {**SINGLE, "error_causes": ["calculation"]})
    assert calc["error_causes"] == ["calculation"]
    other = await _create(
        client, {**SINGLE, "stem": r"另一题：$\cos x$ 的导数是什么？", "answer": "A"}
    )
    await client.patch(
        f"/api/v1/questions/{other['id']}", json={"error_causes": ["concept_unclear"]}
    )

    filtered = (await client.get("/api/v1/questions", params={"error_cause": "calculation"})).json()
    assert [item["id"] for item in filtered["questions"]] == [calc["id"]]
    assert (await client.get("/api/v1/questions", params={"error_cause": "memory_weak"})).json()[
        "questions"
    ] == []


async def test_similar_questions_orders_and_excludes_self(bank):
    client, _app = bank
    base = await _create(client, SINGLE)
    near = await _create(client, {**SINGLE, "stem": "函数 $f(x)=x^2$ 在 $x=3$ 处的导数是多少？"})
    await _create(client, SHORT)  # 无关题：相似度过低被滤掉

    body = (await client.get(f"/api/v1/questions/{base['id']}/similar")).json()
    assert body["question_id"] == base["id"]
    assert body["low_confidence"] is False
    ids = [item["question"]["id"] for item in body["items"]]
    assert base["id"] not in ids  # 排除自身
    assert ids == [near["id"]]  # 无关题被 min_score 滤掉
    # 只换了题面里一个数字，相似度 0.9565——超过 0.8 判重阈值。
    # 这正是「采纳路径不接查重、相似度只做提示」的现实注脚（见 dedup 模块注释）。
    assert body["items"][0]["score"] == pytest.approx(0.9565, abs=0.005)

    high = (
        await client.get(f"/api/v1/questions/{base['id']}/similar", params={"min_score": 0.99})
    ).json()
    assert high["items"] == []
    assert (await client.get("/api/v1/questions/q-nope/similar")).status_code == 404


async def test_similar_flags_low_confidence_for_short_stems(bank):
    client, _app = bank
    tiny = await _create(client, {**SINGLE, "stem": "1+1=?", "options": ["1", "2"], "answer": "B"})
    body = (await client.get(f"/api/v1/questions/{tiny['id']}/similar")).json()
    assert body["low_confidence"] is True  # token 太少：分数仅供参考


async def test_classify_writes_back_and_records_usage(bank, repo_prompts):
    client, app = bank
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[CLASSIFY_REPLY], usage={"prompt_tokens": 50, "completion_tokens": 20}
            )
        ]
    )
    install_scripted(lambda: scripted)
    created = await _create(client, {**SINGLE, "tags": ["考试"]})
    await client.patch(
        f"/api/v1/questions/{created['id']}", json={"note": "又把导数公式记成了 $2x^2$"}
    )

    response = await client.post(
        f"/api/v1/questions/{created['id']}/classify", json={"language": "zh"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggestion"]["error_causes"] == ["concept_unclear"]
    question = body["question"]
    assert question["knowledge_point"] == "幂函数求导"  # 非空 → 覆盖
    assert question["tags"] == ["考试", "易错"]  # 取并集，用户自己的标签不动
    assert question["error_causes"] == ["concept_unclear"]
    assert scripted.exhausted

    # 笔记进了提示词（不看笔记就分类，feature 就是假的）
    assert "2x^2" in scripted.calls[0].messages[-1]["content"]

    # 成本按合成 turn_id 记账（非回合通路，同判分那条先例）
    rows = await app.state.db.fetch_all(
        "SELECT session_id, turn_id, input_tokens FROM usage_records"
    )
    assert len(rows) == 1
    assert rows[0]["turn_id"].startswith("classify-qrun-")
    assert rows[0]["input_tokens"] == 50
    assert rows[0]["session_id"] == ""


async def test_classify_bad_reply_is_502(bank, repo_prompts):
    client, _app = bank
    scripted = ScriptedLLM([ScriptedStep(chunks=["今天天气不错，不适合分类。"])])
    install_scripted(lambda: scripted)
    created = await _create(client, SINGLE)
    response = await client.post(f"/api/v1/questions/{created['id']}/classify", json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_output_invalid"
    assert response.json()["error"]["recoverable"] is True


async def test_classify_without_model_is_503(bank):
    client, _app = bank

    def _no_model():
        raise LLMConfigError("没有可用的模型配置")

    install_scripted(_no_model)
    created = await _create(client, SINGLE)
    response = await client.post(f"/api/v1/questions/{created['id']}/classify", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert response.json()["error"]["recoverable"] is True


async def test_variants_ai_preview_only_and_bad_drafts_dropped(bank, repo_prompts):
    client, app = bank
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[VARIANTS_REPLY], usage={"prompt_tokens": 80, "completion_tokens": 60}
            )
        ]
    )
    install_scripted(lambda: scripted)
    parent = await _create(client, SINGLE)
    before = (await client.get("/api/v1/questions")).json()["counts"]["all"]

    response = await client.post(
        f"/api/v1/questions/{parent['id']}/variants",
        json={"mode": "ai", "count": 3, "language": "zh"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["origin"] == "ai" and body["degraded"] is False and body["sources"] == []
    assert len(body["variants"]) == 1  # 坏草稿（答案读不准）丢弃
    draft = body["variants"][0]
    assert draft["answer"] == "B"  # 选项原文唯一命中 → 归一成标签
    assert draft["knowledge_point"] == "导数"  # 知识点继承原题
    assert draft["duplicate_score"] > 0  # 与原题像不像的提示分（不拦截）
    assert scripted.exhausted

    # 预览不落库；采纳走 /questions/batch
    assert (await client.get("/api/v1/questions")).json()["counts"]["all"] == before

    rows = await app.state.db.fetch_all("SELECT turn_id FROM usage_records")
    assert rows[0]["turn_id"].startswith("variants-qrun-")


async def test_variants_all_bad_drafts_is_502(bank, repo_prompts):
    client, _app = bank
    scripted = ScriptedLLM([ScriptedStep(chunks=['{"variants": [{"stem": "", "answer": "x"}]}'])])
    install_scripted(lambda: scripted)
    created = await _create(client, SINGLE)
    response = await client.post(f"/api/v1/questions/{created['id']}/variants", json={"mode": "ai"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_output_invalid"


class _StubKB:
    """鸭子类型替身：只实现被调到的 has_ready_kb / search_all_ready 两个接口。"""

    def __init__(self, *, ready: bool = True) -> None:
        self._ready = ready
        self.queries: list[str] = []

    def has_ready_kb(self) -> bool:
        return self._ready

    async def search_all_ready(self, query, *, mode="hybrid", top_k=5):
        self.queries.append(query)
        return [
            Hit(
                kb_id="kb-1",
                doc_id="kbdoc-1",
                score=0.9,
                page=3,
                text="素材：导数的定义是函数在一点处的瞬时变化率……",
                metadata={"kb_name": "高数笔记", "filename": "导数.pdf"},
            )
        ]


async def test_variants_grounded_uses_kb_material(bank, repo_prompts):
    client, app = bank
    scripted = ScriptedLLM(
        [
            ScriptedStep(
                chunks=[VARIANTS_REPLY], usage={"prompt_tokens": 100, "completion_tokens": 80}
            )
        ]
    )
    install_scripted(lambda: scripted)
    stub = _StubKB()
    app.state.kb = stub
    parent = await _create(client, {**SINGLE, "knowledge_point": "导数"})

    response = await client.post(
        f"/api/v1/questions/{parent['id']}/variants", json={"mode": "auto", "count": 2}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["origin"] == "kb" and body["degraded"] is False
    assert stub.queries == ["导数"]  # 查询词优先用知识点
    assert body["sources"][0]["kb_name"] == "高数笔记"
    assert body["sources"][0]["filename"] == "导数.pdf"
    # 素材确实进了提示词（grounded 分支）
    assert "瞬时变化率" in scripted.calls[0].messages[-1]["content"]


async def test_variants_degrade_to_ai_without_ready_kb(bank, repo_prompts):
    client, app = bank
    scripted = ScriptedLLM([ScriptedStep(chunks=[VARIANTS_REPLY])])
    install_scripted(lambda: scripted)
    app.state.kb = _StubKB(ready=False)
    parent = await _create(client, SINGLE)

    response = await client.post(f"/api/v1/questions/{parent['id']}/variants", json={"mode": "kb"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["origin"] == "ai" and body["degraded"] is True and body["sources"] == []

    bad_mode = await client.post(
        f"/api/v1/questions/{parent['id']}/variants", json={"mode": "magic"}
    )
    assert bad_mode.status_code == 422


async def test_batch_adopt_marks_variant_source(bank):
    client, _app = bank
    parent = await _create(client, SINGLE)
    response = await client.post(
        "/api/v1/questions/batch",
        json={
            "parent_id": parent["id"],
            "questions": [
                {**SINGLE, "stem": "变式一：$f(x)=x^3$ 在 $x=1$ 处的导数？", "answer": "A"},
                {**SINGLE, "stem": "变式二：$f(x)=x^3$ 在 $x=2$ 处的导数？", "answer": "B"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    created = response.json()["questions"]
    assert len(created) == 2
    assert all(item["source"] == "variant" for item in created)
    assert all(item["parent_id"] == parent["id"] for item in created)

    before = (await client.get("/api/v1/questions")).json()["counts"]["all"]

    # parent 不存在 → 404，一道都不写
    missing = await client.post(
        "/api/v1/questions/batch", json={"parent_id": "q-nope", "questions": [SINGLE]}
    )
    assert missing.status_code == 404

    # 超 20 道 → 422；非法一道 → 422 且全不写（全有或全无）
    too_many = await client.post("/api/v1/questions/batch", json={"questions": [SINGLE] * 21})
    assert too_many.status_code == 422
    mixed = await client.post(
        "/api/v1/questions/batch",
        json={
            "parent_id": parent["id"],
            "questions": [{**SINGLE, "stem": "好题"}, {**SINGLE, "stem": "  "}],
        },
    )
    assert mixed.status_code == 422
    empty = await client.post("/api/v1/questions/batch", json={"questions": []})
    assert empty.status_code == 422

    assert (await client.get("/api/v1/questions")).json()["counts"]["all"] == before
