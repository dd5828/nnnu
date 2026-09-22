"""嵌入提供者契约与错误类型（§4 / §7.9）。

provider 只管「一批文本 → 一批等长向量」，批量切分、进度回调、取消检查、
限流重试都在 EmbeddingService 层统一编排——各 provider 不需要各写一遍。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class EmbeddingError(RuntimeError):
    """嵌入失败（重试耗尽、端点报错、模型不可用）。"""


class ModelDownloadError(RuntimeError):
    """本地模型下载失败（网络不通或源都不可用）。"""


class BuildCancelled(Exception):
    """构建被用户取消（kb 管线在批间检查取消标志后抛出）。"""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """嵌入提供者：本地 ONNX 与远程 OpenAI 兼容端点都实现它。"""

    label: str  # 进 embedding_signature：换模型/端点 = 换签名 = 需要重建索引
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """一批文本 → 一批 L2 归一化向量（顺序对应）。"""
