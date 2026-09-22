"""离线嵌入替身（NNNU_EMBEDDING_MOCK=scripted）：确定性词袋 hash 向量。

E2E 和手工冒烟不该把「模型下载 + 真推理」拖进来：这个替身让整条索引管线
（切块→分批嵌入→落盘→检索→引用）真跑一遍，只有语义是假的。做法是按词
取 hash 得到方向、同向叠加再归一化——同一段文字必得同一向量；文字有词面
重叠时余弦也真的更高，所以「搜到的词就在第几页」这类断言站得住。

替身自带一份极简分词，不引 rag.bm25：替身依赖被测代码就不是替身了。
"""

from __future__ import annotations

import hashlib
import math
import re

DIM = 512  # 与 bge-small-zh-v1.5 对齐，切换真模型不用改索引格式

_CJK_RUN = re.compile(r"[一-鿿]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """汉字出单字 + 相邻二字词，ASCII 出整词——够用就行，不追精度。"""
    lowered = text.lower()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_ASCII_WORD.findall(lowered))
    return tokens


def embed_text(text: str, dim: int = DIM) -> list[float]:
    """一段文本 → L2 归一化向量（同文本必同向量，空文本给个固定方向兜底）。"""
    vector = [0.0] * dim
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        vector[0] = 1.0  # 无词可哈希：给个固定方向，别造零向量（余弦会除零）
        return vector
    return [value / norm for value in vector]


class ScriptedEmbedder:
    """实现 EmbeddingProvider 契约：进程内单例，零依赖零网络。"""

    label = "stub:scripted"
    dim = DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [embed_text(text, self.dim) for text in texts]
