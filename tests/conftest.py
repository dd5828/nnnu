"""共享夹具：临时 home 目录 + httpx 测试客户端（跑 lifespan）+ 嵌入测试替身。"""

import hashlib
import math

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """把 NNNU_HOME 指向临时目录，隔离真实 data/。"""
    monkeypatch.setenv("NNNU_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
async def client(tmp_home):
    from nnnu.api.main import create_app

    app = create_app()
    # httpx ASGITransport 不自动跑 lifespan，手动进入 lifespan 上下文
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


class HashEmbedder:
    """确定性 hash 嵌入（离线测试用）：同文本同向量、异文本近似正交。"""

    label = "stub:hash"

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [float(digest[i % len(digest)]) - 128.0 for i in range(self.dim)]
            vectors.append(_normalize(raw))
        return vectors


class TopicEmbedder:
    """按主题词命中的「语义」嵌入：与 BM25 的词面信号刻意错开。

    用来构造「混合检索结果 ≠ 纯向量结果且各有道理」的可断言语料（验收 B）：
    纯词面的文档 BM25 靠前，同主题但换词的文档向量靠前，融合后两者都在。
    """

    label = "stub:topic"

    def __init__(self, topics: dict[str, tuple[str, ...]] | None = None) -> None:
        self.topics = topics or {
            "signal": ("傅里叶", "频率", "频谱", "信号", "滤波", "时域", "变换"),
            "bio": ("光合作用", "植物", "叶绿体", "细胞"),
        }
        self.dim = len(self.topics)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            raw = [
                float(sum(1 for word in words if word in text)) for words in self.topics.values()
            ]
            if not any(raw):
                raw[0] = 0.1  # 都不命中：给个非零兜底，避免零向量
            vectors.append(_normalize(raw))
        return vectors
