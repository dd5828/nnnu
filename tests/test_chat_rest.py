"""REST 兜底：非流式单回合与 busy 409。"""

import asyncio
import threading

import pytest

from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMChunk, LLMRequest
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


async def test_nonstream_turn_roundtrip(client):
    install_scripted(
        lambda: ScriptedLLM(
            [
                ScriptedStep(
                    chunks=["REST 回答"], usage={"prompt_tokens": 10, "completion_tokens": 5}
                )
            ]
        )
    )
    resp = await client.post("/api/v1/chat", json={"message": "问题"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["response"] == "REST 回答"
    assert data["status"] == "completed"
    assert data["turn_id"].startswith("turn-")
    assert data["session_id"].startswith("sess-")
    assert data["cost_summary"]["tokens"] == 15


async def test_busy_409(client):
    release = threading.Event()

    class SlowClient:
        async def complete(self, request: LLMRequest):
            raise NotImplementedError

        async def stream(self, request: LLMRequest):
            yield LLMChunk(text="部分")
            await asyncio.to_thread(release.wait)
            yield LLMChunk(finish_reason="stop")

    install_scripted(lambda: SlowClient())
    # 发起后需要等回合注册（同一 session 并发）
    task = asyncio.create_task(
        client.post("/api/v1/chat", json={"session_id": "sess-rest", "message": "第一个"})
    )
    await asyncio.sleep(0.3)
    resp = await client.post("/api/v1/chat", json={"session_id": "sess-rest", "message": "第二个"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "session_busy"
    assert resp.json()["error"]["recoverable"] is True
    release.set()
    await task
