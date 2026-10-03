"""题目查重（§7.4「与已有题库比对避免重复」）：分词集合的 Jaccard 相似度。

用 token 集合而不是原文字符串，是为了扛住换词序、改标点、加空格；分词直接复用
rag 的词法器（`services/rag/bm25.py:tokenize`，中文单字 + 二元组，ASCII 整词），
一套中文分法在检索和查重之间不搞两份。

短题护栏：参与查重的文本 token 数少于 MIN_TOKENS 直接判为「不重复」——
两三个词的题面（「求导」）和什么题都像，硬判会误杀。

另有一组**只排序不判重**的辅助（相似题列表用）：`ranked_similar` / `low_confidence`。
注意「同模板换数字」的变式题相似度天然高达 0.85+，会被 `is_duplicate`（0.8）误杀——
所以采纳变式题**绝不**走 is_duplicate，相似度只用来给前端出「可能重题」的提示。
"""

from typing import Iterable, Sequence

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


def token_count(text: str) -> int:
    """文本的 token 集合大小（查重与相似排序同一口径）。"""
    return len(set(tokenize(text)))


def low_confidence(text: str) -> bool:
    """文本太短，相似度数字仅供参考（与 is_duplicate 的短题护栏同一口径）。"""
    return token_count(text) < MIN_TOKENS


def ranked_similar(
    target: str,
    candidates: Sequence[str],
    *,
    min_score: float = 0.15,
    limit: int = 5,
) -> list[tuple[int, float]]:
    """按 Jaccard 给候选排相似度，返回 (下标, 分数) 降序。

    与 is_duplicate 的分工：那是「是不是同一道题」的阈值判据（≥0.8 才算重复），
    这是「像不像」的排序——任何分数都行，只把低于 min_score 的当噪声滤掉。
    目标文本无 token（空题面）才返回空；短文本照排，准不准由调用方看
    `low_confidence` 决定要不要提示。
    """
    target_tokens = set(tokenize(target))
    if not target_tokens:
        return []
    scored: list[tuple[int, float]] = []
    for index, candidate in enumerate(candidates):
        tokens = set(tokenize(candidate))
        if not tokens:
            continue
        score = len(target_tokens & tokens) / len(target_tokens | tokens)
        if score >= min_score:
            scored.append((index, round(score, 4)))
    # 同分按原序（库里新旧顺序），结果可复现
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[: max(0, limit)]
