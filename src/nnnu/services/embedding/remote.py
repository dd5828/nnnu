"""远程嵌入（§7.9）：任一套 OpenAI 兼容的 /embeddings 端点。

复用 OpenAICompatClient：认证头、超时、状态码→typed 异常的错误映射都现成，
这里只负责拼 base_url、把返回值归一化，以及「维度是第一次调用才知道的」
这件事——本地模型维度写死在导出文件里，远程端点得问一次才准。

替换 base_url 时请带上版本路径（如 https://api.siliconflow.cn/v1），
与设置页模型卡片的 base_url 同一套约定。
"""

from __future__ import annotations

import logging

import httpx

from nnnu.services.embedding.base import EmbeddingError
from nnnu.services.llm.openai_compat import OpenAICompatClient
from nnnu.services.llm.provider_registry import ProviderSpec

logger = logging.getLogger(__name__)


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class RemoteEmbeddingProvider:
    """实现 EmbeddingProvider 契约；密钥只在这一层经手，不进任何落盘文件。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        dim: int = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim  # 0 = 还没调过，第一次响应回来才定
        self.label = f"remote:{self.base_url}:{model}"
        spec = ProviderSpec(id="embedding", label="嵌入端点", base_url=self.base_url)
        self._client = OpenAICompatClient(spec, model, api_key=api_key, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = await self._client.embed(texts)
        if len(vectors) != len(texts):
            raise EmbeddingError(f"嵌入端点返回 {len(vectors)} 条，请求了 {len(texts)} 条")
        widths = {len(vector) for vector in vectors}
        if len(widths) != 1:
            raise EmbeddingError(f"嵌入端点返回的向量长度不一致：{sorted(widths)}")
        self.dim = widths.pop()
        return [_normalize(vector) for vector in vectors]
