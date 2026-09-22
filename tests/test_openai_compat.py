"""OpenAI 兼容客户端：SSE 解析、tool_calls 累积、usage 非零帧、think 拆分。"""

import json

import httpx
import pytest

from nnnu.services.llm.errors import (
    LLMAuthenticationError,
    LLMError,
    LLMRateLimitError,
    LLMTransportError,
)
from nnnu.services.llm.openai_compat import InlineThinkFilter, OpenAICompatClient
from nnnu.services.llm.protocol import LLMRequest
from nnnu.services.llm.provider_registry import build_registry, find_by_id

SPEC = find_by_id(build_registry(), "deepseek")


def _transport(lines: list[str], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {line}\n\n" for line in lines)
        return httpx.Response(
            status,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    return httpx.MockTransport(handler)


def _sse_delta(content: str | None = None, finish: str | None = None, **extra) -> str:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    delta.update(extra)
    payload = {"choices": [{"delta": delta}]}
    if finish:
        payload["choices"][0]["finish_reason"] = finish
    return json.dumps(payload, ensure_ascii=False)


async def _collect_text(client, request) -> str:
    out = ""
    async for chunk in client.stream(request):
        out += chunk.text
    return out


def _make_client(lines: list[str], **kwargs) -> OpenAICompatClient:
    return OpenAICompatClient(
        SPEC, "deepseek-chat", api_key="k", transport=_transport(lines), **kwargs
    )


async def test_sse_text_deltas_coalesced():
    client = _make_client(
        [
            _sse_delta("a" * 30),
            _sse_delta("b" * 30),
            _sse_delta("c" * 30),
            _sse_delta("尾", finish="stop"),
        ]
    )
    chunks = [c async for c in client.stream(LLMRequest(messages=[], model="deepseek-chat"))]
    text_chunks = [c.text for c in chunks if c.text]
    # 未满 64 字符不冲刷；达到阈值整块冲出（前 60 字符不满 → 90 字符才出块）
    assert text_chunks[0] == "a" * 30 + "b" * 30 + "c" * 30
    assert text_chunks[1] == "尾"
    assert chunks[-1].finish_reason == "stop"
    await client.aclose()


async def test_sse_small_deltas_flushed_at_end():
    client = _make_client([_sse_delta("小"), _sse_delta("尾", finish="stop")])
    chunks = [c async for c in client.stream(LLMRequest(messages=[], model="deepseek-chat"))]
    # 流尾强制冲刷，小尾巴不丢
    assert [c.text for c in chunks if c.text] == ["小尾"]
    await client.aclose()


async def test_tool_call_accumulated_across_chunks():
    lines = [
        _sse_delta(
            tool_calls=[
                {"index": 0, "id": "call-1", "function": {"name": "add", "arguments": '{"a"'}}
            ]
        ),
        _sse_delta(tool_calls=[{"index": 0, "function": {"arguments": ": 1}"}}]),
        _sse_delta(finish="tool_calls"),
    ]
    client = _make_client(lines)
    response = await client.complete(LLMRequest(messages=[], model="deepseek-chat"))
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call-1"
    assert call.name == "add"
    assert call.arguments == '{"a": 1}'
    await client.aclose()


async def test_usage_nonzero_frames_only():
    lines = [
        _sse_delta("hi"),
        json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        ),  # 全零帧
        json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        " [DONE]",
    ]
    client = _make_client(lines)
    response = await client.complete(LLMRequest(messages=[], model="deepseek-chat"))
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert response.text == "hi"
    await client.aclose()


async def test_think_tag_split_closed_and_unclosed():
    client = _make_client(
        [
            _sse_delta("<think>推理中"),
            _sse_delta("..."),
            _sse_delta("</think>答案部分"),
            _sse_delta("更多"),
        ]
    )
    response = await client.complete(LLMRequest(messages=[], model="deepseek-chat"))
    assert response.thinking == "推理中..."
    assert response.text == "答案部分更多"
    await client.aclose()

    # 未闭合：残留按 thinking 释放，不泄漏进正文
    client2 = _make_client([_sse_delta("<think>没写完"), _sse_delta("的推理")])
    response2 = await client2.complete(LLMRequest(messages=[], model="deepseek-chat"))
    assert response2.thinking == "没写完的推理"
    assert response2.text == ""
    await client2.aclose()


async def test_reasoning_content_field():
    lines = [
        json.dumps({"choices": [{"delta": {"reasoning_content": "想"}}]}),
        _sse_delta("答"),
    ]
    client = _make_client(lines)
    response = await client.complete(LLMRequest(messages=[], model="deepseek-chat"))
    assert response.thinking == "想"
    assert response.text == "答"
    await client.aclose()


async def test_done_sentinel_terminates():
    client = _make_client([_sse_delta("a"), " [DONE]"])
    chunks = [c async for c in client.stream(LLMRequest(messages=[], model="deepseek-chat"))]
    assert [c.text for c in chunks if c.text] == ["a"]
    await client.aclose()


async def test_error_status_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client = OpenAICompatClient(SPEC, "m", api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMRateLimitError):
        await client.complete(LLMRequest(messages=[], model="m"))
    await client.aclose()

    def handler401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid key")

    client401 = OpenAICompatClient(
        SPEC, "m", api_key="k", transport=httpx.MockTransport(handler401)
    )
    with pytest.raises(LLMAuthenticationError):
        await client401.complete(LLMRequest(messages=[], model="m"))
    await client401.aclose()

    def handler500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    client500 = OpenAICompatClient(
        SPEC, "m", api_key="k", transport=httpx.MockTransport(handler500)
    )
    with pytest.raises(LLMTransportError):
        await client500.complete(LLMRequest(messages=[], model="m"))
    await client500.aclose()


async def test_stream_error_body_reaches_mapping():
    """流式拿到非 200 时先读 body 再抛：上游写的原因（比如 422 的 details）要能带出来。

    真实网络下流式响应体是没读过的，直接取 text 会抛 ResponseNotRead，
    错误信息就只剩「崩溃」而不是「422 + 原因」。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = b'{"error": {"message": "tool_calls must be followed by tool messages"}}'
        return httpx.Response(422, stream=httpx.ByteStream(body))

    client = OpenAICompatClient(SPEC, "m", api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError) as info:
        async for _ in client.stream(LLMRequest(messages=[], model="m")):
            pass
    assert "422" in str(info.value)
    assert "tool_calls must be followed" in str(info.value)
    await client.aclose()


def test_inline_think_filter_unit():
    filt = InlineThinkFilter()
    assert filt.feed("正文<think>思考") == ("", "正文")
    assert filt.feed("中</think>继续") == ("思考中", "继续")
    assert filt.flush() == ("", "")

    filt2 = InlineThinkFilter()
    # <thinking> 变体归一化处理
    assert filt2.feed("a<thinking>变体</thinking>b") == ("变体", "ab")
    assert filt2.feed("c") == ("", "c")
