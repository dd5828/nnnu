"""错误映射与重试（§6.10）：把 SDK/httpx 异常转成 typed LLMError 层级。

- 已是 LLMError 原样返回（只补 provider）——不把精确错误重分类成模糊错误；
- 401 → Authentication；429 → RateLimit（解析 Retry-After）；5xx → Transport；
- 400 类检查上下文超长关键词 → ContextWindow；
- httpx 超时/异常链内传输错误 → Timeout/Transport；
- 未知异常 → 不可重试 LLMError（不掩盖 bug）；
- 错误体截断 2000 字符（provider 会把请求回显进错误体，防日志爆炸）。
"""

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

import httpx

from nnnu.services.llm.errors import (
    LLMAuthenticationError,
    LLMContextWindowError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransportError,
)

logger = logging.getLogger(__name__)

MAX_ERROR_BODY_CHARS = 2000
CONTEXT_WINDOW_MARKERS = ("context length", "maximum context", "context_length_exceeded")

T = TypeVar("T")


def _truncate(text: str) -> str:
    return text[:MAX_ERROR_BODY_CHARS]


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Retry-After：数字秒或 HTTP 日期。"""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def map_error(exc: Exception, provider: str | None = None) -> LLMError:
    if isinstance(exc, LLMError):
        if provider is not None and exc.provider is None:
            exc.provider = provider
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = _truncate(exc.response.text)
        if status == 401 or status == 403:
            return LLMAuthenticationError(f"{status} 密钥无效或无权限: {body}", provider=provider)
        if status == 429:
            return LLMRateLimitError(
                f"429 限流: {body}",
                retry_after=retry_after_seconds(exc.response),
                provider=provider,
            )
        if status >= 500:
            return LLMTransportError(f"provider {status}: {body}", provider=provider)
        lowered = body.lower()
        if any(marker in lowered for marker in CONTEXT_WINDOW_MARKERS):
            return LLMContextWindowError(f"上下文超长: {body}", provider=provider)
        return LLMError(f"LLM API {status}: {body}", provider=provider)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return LLMTimeoutError(f"请求超时: {exc}", provider=provider)
    # 异常链遍历：httpx/httpcore 的传输层失败（连接重置等）均按可恢复处理
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, httpx.TimeoutException):
            return LLMTimeoutError(f"请求超时: {cause}", provider=provider)
        if isinstance(cause, httpx.TransportError):
            return LLMTransportError(f"传输失败: {cause}", provider=provider)
        cause = cause.__cause__
    return LLMError(f"未知 LLM 错误: {exc}", provider=provider)


async def with_retries(
    call: Callable[[], Awaitable[T]], *, retries: int = 2, base_delay: float = 1.0
) -> T:
    """429 指数退避重试 2 次（§6.10）；其余 LLMError 直抛不重试。"""
    attempt = 0
    while True:
        try:
            return await call()
        except LLMRateLimitError as exc:
            if attempt >= retries:
                raise
            delay = exc.retry_after if exc.retry_after is not None else base_delay * (2**attempt)
            logger.warning("429 限流，%.1fs 后第 %d/%d 次重试", delay, attempt + 1, retries)
            await asyncio.sleep(delay)
            attempt += 1
