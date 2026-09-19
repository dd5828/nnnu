"""成本汇总 API（验收④）：usage_records 落库与 days 过滤。"""

import pytest

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


async def test_cost_summary_persisted_to_usage_records(client):
    install_scripted(
        lambda: ScriptedLLM(
            [
                ScriptedStep(
                    chunks=["答"],
                    usage={"prompt_tokens": 100, "completion_tokens": 20},
                )
            ]
        )
    )
    resp = await client.post("/api/v1/chat", json={"session_id": "sess-cost", "message": "问"})
    assert resp.status_code == 200

    summary = await client.get("/api/v1/cost/summary?days=7")
    assert summary.status_code == 200
    data = summary.json()
    assert data["total_tokens"] == 120
    assert data["per_model"][0]["model"] == "deepseek-chat"
    assert data["per_model"][0]["calls"] == 1
    assert data["per_model"][0]["input_tokens"] == 100
    assert len(data["by_day"]) == 1
    # 成本按价格表估算（deepseek-chat 0.00014/0.00028 每 1K）
    expected_cost = round((100 * 0.00014 + 20 * 0.00028) / 1000, 6)
    assert abs(data["total_cost"] - expected_cost) < 1e-9


async def test_query_days_filter(client):
    install_scripted(lambda: ScriptedLLM([ScriptedStep(chunks=["答"])]))
    await client.post("/api/v1/chat", json={"session_id": "sess-cost2", "message": "问"})
    empty = await client.get("/api/v1/cost/summary?days=0")
    assert empty.json()["total_tokens"] == 0
