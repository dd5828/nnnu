"""题库 REST（§9.1 + 本批新增的判分端点）：CRUD、三个筛选与计数、作答判分。

判分覆盖两条路：客观题确定性判分（不打模型），简答没把握时走 LLM 判分器
（脚本化替身），用量按合成 turn_id 写 usage_records。

用自带 app 句柄的 client（照 test_model_selection 的 client_app）：好几处断言
要绕过 API 直接读库（作答表、级联删除、成本表）。
"""

import pytest
from httpx import ASGITransport, AsyncClient

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.question_bank.service import RECENCY_WEIGHTS

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
