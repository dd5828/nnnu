"""题目查重（§7.4「与已有题库比对避免重复」）：分词集合的 Jaccard 相似度。

用 token 集合而不是原文字符串，是为了扛住换词序、改标点、加空格；分词直接复用
rag 的词法器（`services/rag/bm25.py:tokenize`，中文单字 + 二元组，ASCII 整词），
一套中文分法在检索和查重之间不搞两份。

短题护栏：参与查重的文本 token 数少于 MIN_TOKENS 直接判为「不重复」——
两三个词的题面（「求导」）和什么题都像，硬判会误杀。
"""

from typing import Iterable

from nnnu.services.rag.bm25 import tokenize

# 阈值 0.8：同一道题换个说法仍会大面积重合，不同题的重叠远低于此
DEFAULT_THRESHOLD = 0.8
MIN_TOKENS = 20


def comparison_text(stem: str, options: Iterable[str] | None = None) -> str:
    """参与查重的文本 = 题面 + 选项（选项改了就算新题）。"""
    parts = [stem or "", *(options or ())]
    return "\n".join(part for part in parts if part)


def similarity(a: str, b: str) -> float:
    """两个文本的 Jaccard 相似度（无 token 时 0）。"""
    tokens_a, tokens_b = set(tokenize(a)), set(tokenize(b))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def is_duplicate(a: str, b: str, *, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """a 是否与 b 重复；a 太短（< MIN_TOKENS）一律不算重复。"""
    if len(set(tokenize(a))) < MIN_TOKENS:
        return False
    return similarity(a, b) >= threshold


def find_duplicate(
    text: str, existing: Iterable[str], *, threshold: float = DEFAULT_THRESHOLD
) -> str | None:
    """在已有文本里找第一个与 text 重复的，返回它；没有则 None。

    调用方把「库里的题」和「本批已接受的题」拼成一个池传进来，
    批量内部去重就是同一件事。
    """
    for candidate in existing:
        if is_duplicate(text, candidate, threshold=threshold):
            return candidate
    return None
