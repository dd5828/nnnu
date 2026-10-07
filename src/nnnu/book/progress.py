"""阅读进度与测验判分（§7.14）：全确定性、零 LLM。

口径（§7.14 没给数的自定项，见 STAGE_LOG 的 P10 登记）：
- 判分：answer 是选项 key 集合；与 answer_key 集合**完全相等**才对（漏选/多选都算错）；
- 完成度：一页「读完」= 已访问 且 页内 quiz 块都有作答（没有 quiz 的页只看已访问）；
- 每块的得分看**最近一次**作答（重做覆盖旧结果，同错题本「最近几次」的口径）；
- 薄弱章：有作答且正确率 < 60% 的章。
"""

from __future__ import annotations

from typing import Any

from nnnu.book.models import Attempt, Block, BookPage

WEAK_ACCURACY = 0.6  # 弱章线：最近一次正确率低于它就进 weak_chapters


def normalize_answer(raw: Any) -> list[str]:
    """作答归一：字符串、大写、去重、保序；空/非列表一律按空集合。"""
    if not isinstance(raw, list):
        return []
    keys: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            key = item.strip().upper()
            if key not in keys:
                keys.append(key)
    return keys


def score_quiz(block: Block, answer: list[str]) -> bool:
    """选项 key 集合完全相等才算对；题目没标答案（理论上不会）按错算。"""
    expected = {str(key).upper() for key in block.payload.get("answer_key") or []}
    return bool(expected) and set(answer) == expected


def latest_by_block(attempts: list[Attempt]) -> dict[str, Attempt]:
    """每块最近一次作答（attempts 按时间正序读，后来的覆盖先前的）。"""
    latest: dict[str, Attempt] = {}
    for attempt in attempts:
        latest[attempt.block_id] = attempt
    return latest


def compute_progress(pages: list[BookPage], attempts: list[Attempt]) -> dict[str, Any]:
    """整书进度读数（书详情与书库列表现算；数据量小，每次全量遍历即可）。"""
    latest = latest_by_block(attempts)
    visited = bookmarked = completed = 0
    quiz_total = quiz_answered = quiz_correct = 0
    weak: list[dict[str, Any]] = []
    for page in pages:
        visited += 1 if page.visited else 0
        bookmarked += 1 if page.bookmarked else 0
        quizzes = [block for block in page.blocks if block.type == "quiz"]
        quiz_total += len(quizzes)
        answered_here = [latest[block.id] for block in quizzes if block.id in latest]
        quiz_answered += len(answered_here)
        correct_here = sum(1 for attempt in answered_here if attempt.correct)
        quiz_correct += correct_here
        if page.visited and len(answered_here) == len(quizzes):
            completed += 1
        if answered_here:
            accuracy = correct_here / len(answered_here)
            if accuracy < WEAK_ACCURACY:
                weak.append(
                    {
                        "chapter_key": page.chapter_key,
                        "page_id": page.id,
                        "answered": len(answered_here),
                        "correct": correct_here,
                        "accuracy": round(accuracy, 3),
                    }
                )
    count = len(pages)
    return {
        "pages": count,
        "visited": visited,
        "bookmarked": bookmarked,
        "completed": completed,
        "completion": round(completed / count, 4) if count else 0.0,
        "quizzes": {"total": quiz_total, "answered": quiz_answered, "correct": quiz_correct},
        "weak_chapters": weak,
    }
