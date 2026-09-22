"""自研 OpenAI 兼容 HTTP 客户端（§6.10）：httpx + SSE，覆盖全部兼容端点。

职责边界（对照参考仓库的 provider 分层）：
- SSE 解析：content / reasoning_content / tool_calls 增量 / usage 帧 / finish_reason；
- tool_calls 按 index 累积（id/name 赋值不拼接、arguments 拼接——防网关每
  chunk 重发 id 导致请求体爆炸的教训）；
- usage 仅记非零帧（防网关每 chunk 回显全零覆盖真实计数），流尾记一次；
- `<think>`/`<thinking>` 内联思考标签增量拆分（未闭合标签残留按思考释放，
  不让推理草稿泄漏进正文）；
- 块合并：正文 ≥64 字符或距上次 flush ≥40ms 才出块（防事件风暴）；
- 错误分类：状态码经 error_mapping 转 typed 异常；超时抛 LLMTimeoutError。
"""

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from nnnu.services.llm.error_mapping import map_error
from nnnu.services.llm.errors import LLMTimeoutError
from nnnu.services.llm.protocol import LLMChunk, LLMRequest, LLMResponse, LLMToolCall
from nnnu.services.llm.provider_registry import ProviderSpec

logger = logging.getLogger(__name__)

MIN_CHUNK_CHARS = 64  # 正文块合并阈值（首块立即发保证低延迟）
FLUSH_INTERVAL_S = 0.04
IDLE_READ_TIMEOUT_S = 90.0  # 每块间空闲超时（防半开连接挂死）

_ACCUMULATE_KEY = "arguments_piece"


class InlineThinkFilter:
    """增量拆分 `<think>`/`<thinking>` 内联思考标签（未闭合也剥，防推理草稿泄漏）。"""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._in_think = False
        self._pending = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        """喂入一段文本，返回 (thinking, text)。"""
        # 归一化 <thinking> 变体
        buffer = chunk.replace("<thinking>", self.OPEN).replace("</thinking>", self.CLOSE)
        buffer = self._pending + buffer
        self._pending = ""
        out_text = ""
        out_thinking = ""
        while buffer:
            if not self._in_think:
                index = buffer.find(self.OPEN)
                if index == -1:
                    out_text += buffer
                    break
                out_text += buffer[:index]
                self._in_think = True
                buffer = buffer[index + len(self.OPEN) :]
            else:
                index = buffer.find(self.CLOSE)
                if index == -1:
                    self._pending = buffer  # 等下一块补齐闭合标签
                    break
                out_thinking += buffer[:index]
                self._in_think = False
                buffer = buffer[index + len(self.CLOSE) :]
        return out_thinking, out_text

    def flush(self) -> tuple[str, str]:
        """流尾：未闭合的思考残留按 thinking 释放（不泄漏进正文）。"""
        if self._in_think:
            leftover = self._pending
            self._pending = ""
            self._in_think = False
            return leftover, ""
        return "", ""


class OpenAICompatClient:
    def __init__(
        self,
        spec: ProviderSpec,
        model: str,
        *,
        api_key: str | None = None,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._spec = spec
        self._model = model
        self._api_key = api_key
        self._timeout = timeout or httpx.Timeout(
            connect=10.0, read=IDLE_READ_TIMEOUT_S, write=30.0, pool=10.0
        )
        # 传输层可注入（测试 MockTransport）；自建客户端由调用方负责关闭
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = LLMResponse()
        calls: list[LLMToolCall] = []
        async for chunk in self.stream(request):
            response.text += chunk.text
            response.thinking += chunk.thinking
            if chunk.tool_call_delta is not None:
                _accumulate_tool_call(calls, chunk.tool_call_delta)
            if chunk.usage:
                response.usage = chunk.usage
            if chunk.finish_reason:
                response.finish_reason = chunk.finish_reason
        response.tool_calls = calls
        return response

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        payload = self._build_payload(request)
        try:
            async with self._client.stream("POST", self._chat_url(), json=payload) as response:
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(
                        f"LLM API {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                async for chunk in self._iter_sse(response):
                    yield chunk
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"流式超时: {exc}", partial_response=True) from exc
        except httpx.HTTPStatusError as exc:
            # 状态码错误统一经错误映射转 typed 异常（调用方按类型决定重试策略）
            raise map_error(exc, provider=self._spec.id) from exc

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """POST /embeddings：批量文本 → 向量（顺序按 data[i].index 还原）。

        P4 嵌入服务复用本客户端，认证/超时/错误映射与聊天完全一致；
        返回的向量**未归一化**（OpenAI 语义如此），归一化由调用方负责。
        """
        payload: dict[str, Any] = {"model": model or self._model, "input": texts}
        try:
            response = await self._client.post(self._embedding_url(), json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise map_error(exc, provider=self._spec.id) from exc
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"嵌入请求超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise map_error(exc, provider=self._spec.id) from exc
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise map_error(
                ValueError(f"嵌入响应缺少 data 字段: {str(body)[:200]}"), provider=self._spec.id
            )
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        return [list(item.get("embedding") or []) for item in ordered]

    def _chat_url(self) -> str:
        return f"{self._base()}/chat/completions"

    def _embedding_url(self) -> str:
        return f"{self._base()}/embeddings"

    def _base(self) -> str:
        return (self._spec.base_url or "").rstrip("/")

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": request.messages,
            "stream": True,
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.reasoning_effort is not None:
            payload["reasoning_effort"] = request.reasoning_effort
        if request.thinking_extra:
            payload["extra_body"] = request.thinking_extra
        return payload

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[LLMChunk]:
        """逐行解析 SSE：data: JSON / data: [DONE]。"""
        think_filter = InlineThinkFilter()
        tool_calls: list[LLMToolCall] = []
        text_buffer = ""
        thinking_buffer = ""
        last_flush = time.monotonic()
        usage: dict[str, int] = {}
        finish_reason: str | None = None

        async def _flush_text() -> AsyncIterator[LLMChunk]:
            nonlocal text_buffer, thinking_buffer, last_flush
            if thinking_buffer:
                yield LLMChunk(thinking=thinking_buffer)
                thinking_buffer = ""
            if text_buffer:
                yield LLMChunk(text=text_buffer)
                text_buffer = ""
            last_flush = time.monotonic()

        async def _emit_tool_calls() -> AsyncIterator[LLMChunk]:
            for index, call in enumerate(tool_calls):
                yield LLMChunk(
                    tool_call_delta={
                        "index": index,
                        "id": call.id,
                        "name": call.name,
                        _ACCUMULATE_KEY: call.arguments,
                    }
                )
            tool_calls.clear()

        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("SSE 行非 JSON，跳过: %.200s", data)
                continue
            chunk = _parse_sse_payload(payload)
            # 文本增量 → think 拆分 → 合并缓冲
            if chunk.text:
                thinking, text = think_filter.feed(chunk.text)
                if thinking:
                    thinking_buffer += thinking
                if text:
                    text_buffer += text
            if chunk.thinking:
                thinking_buffer += chunk.thinking
            if chunk.tool_call_delta is not None:
                _accumulate_tool_call(tool_calls, chunk.tool_call_delta)
            if chunk.usage:
                usage = {**usage, **chunk.usage}
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            # 字符阈值或时间阈值冲刷
            if len(text_buffer) >= MIN_CHUNK_CHARS or (
                (text_buffer or thinking_buffer)
                and time.monotonic() - last_flush >= FLUSH_INTERVAL_S
            ):
                async for piece in _flush_text():
                    yield piece
        # 流尾：工具调用一次性成块给出（累积完成）
        async for piece in _emit_tool_calls():
            yield piece
        # 未闭合思考残留按 thinking 释放
        leftover_thinking, leftover_text = think_filter.flush()
        if leftover_thinking:
            thinking_buffer += leftover_thinking
        if leftover_text:
            text_buffer += leftover_text
        async for piece in _flush_text():
            yield piece
        if usage:
            yield LLMChunk(usage=usage)
        if finish_reason:
            yield LLMChunk(finish_reason=finish_reason)


def _parse_sse_payload(payload: dict[str, Any]) -> LLMChunk:
    """单个 SSE JSON → LLMChunk（choices[0] 的 delta 与 usage 帧）。"""
    choices = payload.get("choices") or []
    chunk = LLMChunk()
    if choices:
        choice = choices[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            chunk.text = content
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str):
            chunk.thinking = reasoning
        raw_calls = delta.get("tool_calls") or []
        if raw_calls:
            raw = raw_calls[0]
            function = raw.get("function") or {}
            chunk.tool_call_delta = {
                "index": int(raw.get("index", 0)),
                "id": raw.get("id"),
                "name": function.get("name"),
                _ACCUMULATE_KEY: function.get("arguments") or "",
            }
        if choice.get("finish_reason"):
            chunk.finish_reason = choice["finish_reason"]
    usage_frame = payload.get("usage")
    if usage_frame:
        nonzero = _nonzero_usage(usage_frame)
        if nonzero:
            chunk.usage = nonzero
    return chunk


def _accumulate_tool_call(accumulator: list[LLMToolCall], delta: dict[str, Any]) -> None:
    """按 index 累积：id/name 赋值不拼接，arguments 拼接。"""
    index = int(delta.get("index", 0))
    while len(accumulator) <= index:
        accumulator.append(LLMToolCall(id="", name="", arguments=""))
    call = accumulator[index]
    if delta.get("id"):
        call.id = delta["id"]
    if delta.get("name"):
        call.name = delta["name"]
    call.arguments += delta.get(_ACCUMULATE_KEY) or ""


def _nonzero_usage(usage_frame: dict[str, Any]) -> dict[str, int]:
    """仅取非零的 prompt/completion tokens（防全零帧覆盖真实计数）。"""
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage_frame.get(key)
        if isinstance(value, int) and value > 0:
            out[key] = value
    return out
