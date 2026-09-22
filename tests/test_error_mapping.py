"""错误映射与重试：typed 分类、Retry-After 解析、429 指数退避。"""

import asyncio
import email.utils

import httpx
import pytest

from nnnu.services.llm.error_mapping import map_error, retry_after_seconds, with_retries
from nnnu.services.llm.errors import (
    LLMAuthenticationError,
    LLMContextWindowError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransportError,
)


def _resp(status: int, body: str = "", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, text=body, headers=headers or {}, request=httpx.Request("POST", "http://x")
    )


def _status_error(
    status: int, body: str = "", headers: dict | None = None
) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"error {status}",
        request=httpx.Request("POST", "http://x"),
        response=_resp(status, body, headers),
    )


def test_map_429_with_retry_after():
    exc = _status_error(429, "limited", {"retry-after": "3"})
    mapped = map_error(exc, provider="deepseek")
    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retry_after == 3.0


def test_retry_after_http_date():
    from datetime import datetime, timezone

    http_date = email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)
    mapped = map_error(_status_error(429, headers={"retry-after": http_date}))
    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retry_after is not None and mapped.retry_after >= 0


def test_map_5xx_transport():
    mapped = map_error(_status_error(503, "overloaded"))
    assert isinstance(mapped, LLMTransportError)
    assert mapped.retryable


def test_map_401_auth():
    assert isinstance(map_error(_status_error(401, "bad key")), LLMAuthenticationError)


def test_map_context_window_marker():
    mapped = map_error(_status_error(400, "maximum context length exceeded"))
    assert isinstance(mapped, LLMContextWindowError)


def test_map_timeout():
    mapped = map_error(httpx.ReadTimeout("read timeout"))
    assert isinstance(mapped, LLMTimeoutError)
    # 异常链里的传输错误也能识别
    chained = RuntimeError("outer")
    chained.__cause__ = httpx.ConnectError("refused")
    assert isinstance(map_error(chained), LLMTransportError)


def test_map_llm_error_passthrough():
    original = LLMRateLimitError("already", retry_after=1.0)
    mapped = map_error(original, provider="deepseek")
    assert mapped is original
    assert mapped.provider == "deepseek"


def test_map_unread_streaming_body_does_not_crash():
    """流式响应没读过 body：映射退化成只有状态码，不能抛 ResponseNotRead 把原因盖掉。"""
    response = httpx.Response(
        422,
        stream=httpx.ByteStream(b'{"error": "bad tool_calls"}'),
        request=httpx.Request("POST", "http://x"),
    )
    mapped = map_error(
        httpx.HTTPStatusError("error 422", request=response.request, response=response)
    )
    assert isinstance(mapped, LLMError)
    assert "422" in str(mapped)


def test_map_unknown_non_network_not_retryable():
    mapped = map_error(ValueError("内部 bug"))
    assert isinstance(mapped, LLMError)
    assert not mapped.retryable


async def test_with_retries_backoff_then_success():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise LLMRateLimitError("limited", retry_after=0)
        return "ok"

    result = await with_retries(flaky, retries=2, base_delay=0.01)
    assert result == "ok"
    assert len(calls) == 3


async def test_with_retries_gives_up():
    async def always_limited():
        raise LLMRateLimitError("limited", retry_after=0)

    with pytest.raises(LLMRateLimitError):
        await with_retries(always_limited, retries=2, base_delay=0.01)


async def test_with_retries_no_retry_on_other_errors():
    calls = []

    async def bad():
        calls.append(1)
        raise LLMTransportError("network down")

    with pytest.raises(LLMTransportError):
        await with_retries(bad, retries=2, base_delay=0.01)
    assert len(calls) == 1
