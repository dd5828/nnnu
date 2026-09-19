"""LLM 异常层级（§6.10 错误映射的产物，typed 结构化重试的依据）。

分类约定（与方案 6.10 对应）：
- LLMRateLimitError（429）：指数退避重试 2 次；
- LLMTimeoutError（超时）：partial_response 携带已收部分，供流式续传；
- LLMContextWindowError（上下文超长）：调用方裁剪预算后重试一次；
- LLMTransportError（5xx 等）：recoverable=true，映射为可恢复 error 事件；
- LLMAuthenticationError / LLMConfigError：不可恢复。
"""


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retryable: bool = False,
        partial_response: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.retryable = retryable
        self.partial_response = partial_response


class LLMConfigError(LLMError):
    """配置错误：缺 key、未知 provider、探测失败。不可重试。"""


class LLMTransportError(LLMError):
    """传输层错误（连接失败/5xx）。retryable=True。"""

    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message, retryable=True, **kwargs)


class LLMTimeoutError(LLMTransportError):
    """超时；partial_response=True 时携带已收部分文本。"""


class LLMRateLimitError(LLMError):
    """429 限流；retry_after 秒数（Retry-After 头，数字或 HTTP 日期）。"""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(message, retryable=True, **kwargs)
        self.retry_after = retry_after


class LLMContextWindowError(LLMError):
    """上下文超长：调用方裁剪预算后重试一次。"""


class LLMAuthenticationError(LLMError):
    """401/密钥无效。不可重试。"""
