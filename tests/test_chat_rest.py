"""REST 兜底：非流式单回合、busy 409、历史回放不含工具轨迹。"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator

import pytest

from nnnu.capabilities.chat.capability import ChatCapability
from nnnu.core.context import SessionRef, UnifiedContext
from nnnu.services.llm.factory import install_scripted, uninstall_scripted
from nnnu.services.llm.protocol import LLMChunk, LLMRequest, LLMToolCall
from nnnu.services.llm.scripted import ScriptedLLM, ScriptedStep
from nnnu.services.sessions.models import Message


@pytest.fixture(autouse=True)
def _clean_llm_injection():
    uninstall_scripted()
    yield
    uninstall_scripted()


class _RecordingLLM(ScriptedLLM):
    """调用当场抄一份消息快照：calls[i].messages 是同一个 list，回合内会被追加污染。"""

    def __init__(self, steps: list[ScriptedStep]) -> None:
        super().__init__(steps)
        self.records: list[list[dict]] = []

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        self.records.append([dict(message) for message in request.messages])
        async for chunk in super().stream(request):
            yield chunk


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


async def test_next_turn_history_has_no_tool_calls(client):
    """上一回合用过工具，下一回合发出去的历史里不能带 tool_calls。

    落库的那份是给界面看的轨迹（tool_name/call_id/ok/summary），形状不是线格式；
    而且带 tool_calls 的 assistant 后面必须紧跟配套的 role="tool"，缺了就 422。
    """
    recorder = _RecordingLLM(
        [
            ScriptedStep(
                tool_calls=[
                    LLMToolCall(
                        id="c1",
                        name="code_execution",
                        arguments=json.dumps({"code": "print(1 + 1)"}),
                    )
                ],
                finish_reason="tool_calls",
            ),
            ScriptedStep(chunks=["算完了，是 2"]),
            ScriptedStep(chunks=["第二问的回答"]),
        ]
    )
    # 每次 create_client 都会调用工厂，返回同一个实例：步序即全局 LLM 调用序
    install_scripted(lambda: recorder)

    first = await client.post("/api/v1/chat", json={"message": "算个 1+1"})
    assert first.status_code == 200
    session_id = first.json()["session_id"]

    second = await client.post(
        "/api/v1/chat", json={"session_id": session_id, "message": "再问一句"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert second.json()["response"] == "第二问的回答"

    # records[2] = 第二回合的首次调用：它的 messages 就是按会话历史拼出来的请求
    sent = recorder.records[2]
    assert [message["role"] for message in sent] == ["system", "user", "assistant", "user"]
    assert not any("tool_calls" in message for message in sent)
    assert sent[1]["content"] == "算个 1+1"
    # 上一回合的正文还在：历史连续性不能因为去掉轨迹而丢
    assert sent[2]["content"] == "算完了，是 2"


def test_session_history_drops_tool_trace_and_empty_assistant():
    """历史回放：轨迹丢掉、纯工具回合的空正文 assistant 也丢掉（都是上游 422 的引信）。"""
    ctx = UnifiedContext(
        session=SessionRef(id="sess-h"),
        capability="chat",
        message=Message.new(session_id="sess-h", role="user", content="当前这问"),
        metadata={
            "session_messages": [
                Message.new(session_id="sess-h", role="user", content="第一问"),
                Message.new(
                    session_id="sess-h",
                    role="assistant",
                    content="",
                    tool_calls=[
                        {"tool_name": "code_execution", "call_id": "c1", "ok": True, "summary": "2"}
                    ],
                ),
                Message.new(
                    session_id="sess-h",
                    role="assistant",
                    content="第一问的答案",
                    tool_calls=[
                        {"tool_name": "code_execution", "call_id": "c1", "ok": True, "summary": "2"}
                    ],
                ),
                Message.new(
                    session_id="sess-h", role="user", content="当前这问"
                ),  # 同 id 过滤那条款
            ]
        },
    )
    ctx.message = ctx.metadata["session_messages"][-1]

    assert ChatCapability._session_history(ctx) == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一问的答案"},
    ]


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
