"""编译估算（§7.14）：确认目录前，先告诉用户这本书大概花多少 token / 多少钱 / 多久。

零 LLM、纯确定性：GET 书详情时按**当前**目录与素材规模现算（改章、删章立即反映，
不在前端镜像公式）。口径：
- 输入 token ≈（每章均分的素材字符 + 每块的提示词固定开销）/ `CHARS_PER_TOKEN`；
- 输出 token 按 `OUTPUT_TOKEN_BUDGET` 的类型预算表；
- 费用按 `lookup_price(当前模型)`；未知模型/本地端点只报 token 数（priced=false）；
- 时间按每类块的经验秒数（题量/图/动画重，小结构块快）。

对照上游 deeptutor/book/estimate.py：它按模板的 target_words 算「一个内容类型一章」
的 basis；我们直接按用户确认过的每章 blocks_plan 逐块算——目录即事实。
"""

from __future__ import annotations

from typing import Any

from nnnu.book.models import CHARS_PER_TOKEN, OUTPUT_TOKEN_BUDGET, BlockPlan, Spine
from nnnu.services.cost.pricing import lookup_price

PROMPT_OVERHEAD_CHARS = 1_200  # 每块系统/用户模板 + 章节信息 + 聚焦点的固定开销（粗估）
CHAPTER_OVERHEAD_CHARS = 400  # 每章一次装配的固定开销

# 每类块的生成经验耗时（秒；单次调用 + 落库）
SECONDS_BY_BLOCK: dict[str, float] = {
    "text": 35.0,
    "callout": 12.0,
    "deep_dive": 45.0,
    "quiz": 20.0,
    "flashcard": 20.0,
    "timeline": 15.0,
    "code": 30.0,
    "figure": 40.0,
    "interactive_html": 45.0,
    "animation": 90.0,
    "concept_graph": 15.0,
    "note": 0.0,  # 手写块零 LLM
}


def _block_numbers(
    plan: BlockPlan, material_tokens: int, price: tuple[float, float] | None
) -> tuple[int, int, float]:
    output_tokens = OUTPUT_TOKEN_BUDGET.get(plan.type, 800)
    if output_tokens == 0:
        return 0, 0, 0.0  # note 是用户手写块：零 LLM，不占预算
    input_tokens = material_tokens + round(PROMPT_OVERHEAD_CHARS / CHARS_PER_TOKEN)
    cost = 0.0
    if price is not None:
        cost = (input_tokens * price[0] + output_tokens * price[1]) / 1000
    return input_tokens, output_tokens, cost


def estimate_book(spine: Spine, *, model: str = "") -> dict[str, Any]:
    """按当前 spine 现算整书估算；spine 没章节时 totals 全零。"""
    price = lookup_price(model) if model else None
    chapters_out: list[dict[str, Any]] = []
    total: dict[str, Any] = {
        "chapters": 0,
        "blocks": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "seconds": 0.0,
    }
    chapter_count = max(1, len(spine.chapters))
    share_chars = spine.material_chars // chapter_count
    material_tokens = round(share_chars / CHARS_PER_TOKEN)

    for chapter in spine.chapters:
        input_tokens = output_tokens = 0
        cost = 0.0
        seconds = round(CHAPTER_OVERHEAD_CHARS / 1000, 1)  # 每章一点固定装配时间
        for plan in chapter.blocks_plan:
            block_in, block_out, block_cost = _block_numbers(plan, material_tokens, price)
            input_tokens += block_in
            output_tokens += block_out
            cost += block_cost
            seconds += SECONDS_BY_BLOCK.get(plan.type, 30.0)
        chapters_out.append(
            {
                "key": chapter.key,
                "title": chapter.title,
                "blocks": len(chapter.blocks_plan),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tokens": input_tokens + output_tokens,
                "cost": round(cost, 6),
                "seconds": round(seconds, 1),
            }
        )
        total["chapters"] += 1
        total["blocks"] += len(chapter.blocks_plan)
        total["input_tokens"] += input_tokens
        total["output_tokens"] += output_tokens
        total["seconds"] += seconds

    total_cost = round(sum(chapter["cost"] for chapter in chapters_out), 6)
    return {
        "model": model,
        "priced": price is not None,
        "basis": {
            "material_chars": spine.material_chars,
            "chars_per_token": CHARS_PER_TOKEN,
            "prompt_overhead_chars": PROMPT_OVERHEAD_CHARS,
        },
        "chapters": chapters_out,
        "totals": {
            **total,
            "tokens": total["input_tokens"] + total["output_tokens"],
            "cost": total_cost,
            "seconds": round(total["seconds"], 1),
        },
    }
