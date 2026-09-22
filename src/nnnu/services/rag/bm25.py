"""BM25 词法检索（§7.9 混合检索的一半）。

分词比 P2 附件的 _tokenize 更细一层：连续汉字展开成**单字 + 二元组**
（单字保召回，二元组保精度），ASCII 字母数字按整词、其它文字按单字。
打分沿用附件检索验证过的 BM25（k1=1.5, b=0.75），与 attachment_search
的唯一差别是查询词先去重（同一个词重复出现不该重复加权）。

索引结构（version-N/bm25.json）：
{chunks: [{chunk_id, doc_id, len, tokens}], df: {term: 块数}, avg_dl, n, k1, b}
词频 Counter 是派生数据，加载时现算，不落盘。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from nnnu.services.rag.base import Chunk

BM25_K1 = 1.5
BM25_B = 0.75

_CJK_RANGES = (("㐀", "䶿"), ("一", "鿿"), ("豈", "﫿"))


def _is_cjk(ch: str) -> bool:
    return any(low <= ch <= high for low, high in _CJK_RANGES)


def _flush_cjk(run: str, tokens: list[str]) -> str:
    """连续汉字 → 单字 + 相邻二元组；返回空串（配合 run = _flush_cjk(run, tokens)）。"""
    if not run:
        return ""
    tokens.extend(run)
    tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return ""


def tokenize(text: str) -> list[str]:
    """中文单字+二元组、ASCII 整词（小写）、其它字母数字单字。"""
    tokens: list[str] = []
    ascii_word = ""
    cjk_run = ""
    for ch in text.lower():
        if ch.isascii() and ch.isalnum():
            cjk_run = _flush_cjk(cjk_run, tokens)
            ascii_word += ch
            continue
        if ascii_word:
            tokens.append(ascii_word)
            ascii_word = ""
        if _is_cjk(ch):
            cjk_run += ch
            continue
        cjk_run = _flush_cjk(cjk_run, tokens)
        if ch.isalnum():  # 假名等非 ASCII 字母数字：单字
            tokens.append(ch)
    _flush_cjk(cjk_run, tokens)
    if ascii_word:
        tokens.append(ascii_word)
    return tokens


class Bm25Index:
    """加载后的 BM25 索引（词频 Counter 常驻内存，查询不做全量扫描）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.k1 = float(payload.get("k1", BM25_K1))
        self.b = float(payload.get("b", BM25_B))
        self.chunks: list[dict[str, Any]] = payload["chunks"]
        self.df: dict[str, int] = payload["df"]
        self.n = int(payload.get("n", len(self.chunks)))
        self.avg_dl = float(payload.get("avg_dl", 0.0))
        self._counters = [Counter(item["tokens"]) for item in self.chunks]

    @classmethod
    def from_chunks(cls, chunks: list[Chunk]) -> Bm25Index:
        records: list[dict[str, Any]] = []
        df: dict[str, int] = {}
        total_len = 0
        for chunk in chunks:
            tokens = tokenize(chunk.text)
            records.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "len": len(tokens),
                    "tokens": tokens,
                }
            )
            total_len += len(tokens)
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1
        n = len(records)
        return cls(
            {
                "k1": BM25_K1,
                "b": BM25_B,
                "chunks": records,
                "df": df,
                "n": n,
                "avg_dl": total_len / n if n else 0.0,
            }
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Bm25Index:
        return cls(payload)

    def to_json(self) -> dict[str, Any]:
        return {
            "k1": self.k1,
            "b": self.b,
            "chunks": self.chunks,
            "df": self.df,
            "n": self.n,
            "avg_dl": self.avg_dl,
        }

    def search(
        self,
        query: str,
        top_k: int,
        *,
        excluded_docs: frozenset[str] = frozenset(),
    ) -> list[tuple[str, float]]:
        """→ [(chunk_id, score)] 按分数降序；无命中返回空列表。"""
        terms = [t for t in dict.fromkeys(tokenize(query)) if t in self.df]
        if not terms or not self.chunks:
            return []
        avg_dl = max(self.avg_dl, 1.0)
        scored: list[tuple[float, str]] = []
        for record, counter in zip(self.chunks, self._counters, strict=True):
            if record["doc_id"] in excluded_docs:
                continue
            score = 0.0
            for term in terms:
                tf = counter.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (self.n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                denom = tf + self.k1 * (1 - self.b + self.b * record["len"] / avg_dl)
                score += idf * tf * (self.k1 + 1) / denom
            if score > 0:
                scored.append((score, record["chunk_id"]))
        scored.sort(key=lambda pair: -pair[0])
        return [(chunk_id, score) for score, chunk_id in scored[:top_k]]
