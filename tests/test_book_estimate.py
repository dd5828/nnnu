"""Book 估算：按当前目录逐块算 token/费用/时间；未知模型只报 token 数。"""

from nnnu.book.estimate import (
    CHAPTER_OVERHEAD_CHARS,
    PROMPT_OVERHEAD_CHARS,
    SECONDS_BY_BLOCK,
    estimate_book,
)
from nnnu.book.models import CHARS_PER_TOKEN, OUTPUT_TOKEN_BUDGET, BlockPlan, Chapter, Spine
from nnnu.services.cost.pricing import lookup_price

MODEL = "deepseek-chat"


def _spine() -> Spine:
    return Spine(
        chapters=[
            Chapter(
                key="ch-1",
                title="第一章",
                blocks_plan=[BlockPlan(type="text"), BlockPlan(type="quiz")],
            ),
            Chapter(
                key="ch-2",
                title="第二章",
                blocks_plan=[BlockPlan(type="flashcard"), BlockPlan(type="note")],
            ),
        ],
        material_chars=7_000,
    )


def test_estimate_counts_match_basis():
    estimate = estimate_book(_spine(), model=MODEL)
    share_tokens = round((7_000 // 2) / CHARS_PER_TOKEN)
    overhead_tokens = round(PROMPT_OVERHEAD_CHARS / CHARS_PER_TOKEN)

    first = estimate["chapters"][0]
    assert first["input_tokens"] == (share_tokens + overhead_tokens) * 2
    assert first["output_tokens"] == OUTPUT_TOKEN_BUDGET["text"] + OUTPUT_TOKEN_BUDGET["quiz"]
    assert first["tokens"] == first["input_tokens"] + first["output_tokens"]
    assert first["seconds"] == round(
        CHAPTER_OVERHEAD_CHARS / 1000 + SECONDS_BY_BLOCK["text"] + SECONDS_BY_BLOCK["quiz"], 1
    )

    totals = estimate["totals"]
    assert totals["chapters"] == 2 and totals["blocks"] == 4
    assert totals["tokens"] == sum(c["tokens"] for c in estimate["chapters"])
    assert totals["seconds"] == round(sum(c["seconds"] for c in estimate["chapters"]), 1)

    # 计价与价格表同口径（deepseek-chat 有价 → priced=True、费用 > 0）
    price = lookup_price(MODEL)
    assert price is not None and estimate["priced"] is True
    expected_cost = sum(
        (c["input_tokens"] * price[0] + c["output_tokens"] * price[1]) / 1000
        for c in estimate["chapters"]
    )
    assert abs(totals["cost"] - expected_cost) < 1e-6
    assert estimate["basis"]["material_chars"] == 7_000


def test_note_block_is_free_and_unknown_model_reports_tokens_only():
    estimate = estimate_book(_spine(), model="我自己的本地模型")
    assert estimate["priced"] is False
    assert estimate["totals"]["cost"] == 0.0
    second = estimate["chapters"][1]
    assert second["output_tokens"] == OUTPUT_TOKEN_BUDGET["flashcard"]  # note 不占输出预算
    assert (
        second["input_tokens"] == estimate["chapters"][0]["input_tokens"] // 2
    )  # 只剩 flashcard 一块
    assert second["seconds"] == round(
        CHAPTER_OVERHEAD_CHARS / 1000 + SECONDS_BY_BLOCK["flashcard"], 1
    )


def test_empty_spine_gives_zero_totals():
    estimate = estimate_book(Spine(), model=MODEL)
    assert estimate["chapters"] == []
    assert estimate["totals"] == {
        "chapters": 0,
        "blocks": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "seconds": 0.0,
        "tokens": 0,
        "cost": 0.0,
    }
