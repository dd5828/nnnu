"""spine 生成（§7.14）：素材 digest → 一次 LLM 调用 → 收编校验（不行就带着问题修复重试一次）。

比上游（spine_agent → source_explorer → spine_synthesizer 的 proposal/exploration/
critique/revise 多段环）压成一次成：一次调用同时产章节树、每章的块计划
（blocks_plan）与全书概念图。校验两档：
- 硬伤（顶层不是对象 / 一个章节都没有 / 块计划全无效 / JSON 洗不出来）→ 带错重试；
- 软问题（缺 title、类型不认识、验收三件套缺项、引用素材清单外的 ref）→ 带问题
  重试一次；第二次仍有就「收下 + 记账」，issues 由服务侧存下、GET 时提示。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nnnu.book.inputs import MaterialDigest
from nnnu.book.llm import BookOutputError, extract_json, one_shot
from nnnu.book.models import BookError, Spine, parse_spine
from nnnu.services.cost.tracker import CostTracker
from nnnu.services.i18n.prompts import get_prompt_manager

MAX_SPINE_ATTEMPTS = 2
SPINE_MAX_TOKENS = 4096  # 章节树 + 块计划 + 概念图，比单块宽一档


@dataclass(slots=True)
class SpineOutcome:
    spine: Spine
    issues: list[str]  # 收编时放宽掉的问题（GET 详情里提示）
    model: str
    usage: dict[str, Any] = field(default_factory=dict)  # CostTracker.summary() 形状


def normalize_language(language: str | None) -> str:
    return language if language in ("zh", "en") else "zh"


def _apply_allowed_refs(spine: Spine, digest: MaterialDigest) -> list[str]:
    """章节引用收编：只留素材清单里出现过的 ref，label 以清单为准（防幻觉引用）。"""
    allowed = digest.ref_labels()
    issues: list[str] = []
    for index, chapter in enumerate(spine.chapters, start=1):
        kept = []
        for ref in chapter.source_refs:
            if ref.ref not in allowed:
                continue
            kept.append(ref.model_copy(update={"label": allowed[ref.ref]}))
        if len(kept) != len(chapter.source_refs):
            issues.append(f"第 {index} 章的 source_refs 里有素材清单外的引用，已丢掉")
        chapter.source_refs = kept
    return issues


async def generate_spine(
    *,
    title: str,
    digest: MaterialDigest,
    language: str = "zh",
    tracker: CostTracker | None = None,
) -> SpineOutcome:
    """跑 spine 生成；模型/JSON 两轮都过不去抛 BookOutputError（路由 502）。"""
    lang = normalize_language(language)
    prompts = get_prompt_manager()
    system = prompts.render(
        "book", lang, "spine.system", block_catalog=prompts.render("book", lang, "block_catalog")
    )
    base_user = prompts.render(
        "book",
        lang,
        "spine.user",
        title=title,
        refs_block=digest.refs_block(lang),
        material=digest.text,
    )

    hint: str | None = None
    last_error = ""
    outcome: SpineOutcome | None = None
    for _attempt in range(MAX_SPINE_ATTEMPTS):
        user = base_user
        if hint is not None:
            user = base_user + "\n\n" + prompts.render("book", lang, "spine.repair", issues=hint)
        result = await one_shot(
            system=system, user=user, max_tokens=SPINE_MAX_TOKENS, tracker=tracker
        )
        model = result.model
        try:
            spine, issues = parse_spine(extract_json(result.text))
        except (BookOutputError, BookError) as exc:
            last_error = str(exc)
            hint = f"- {exc}"
            continue
        spine.language = lang
        spine.material_chars = digest.chars
        issues = [*issues, *_apply_allowed_refs(spine, digest)]
        outcome = SpineOutcome(
            spine=spine,
            issues=issues,
            model=model,
            usage=tracker.summary() if tracker is not None else {},
        )
        if not issues:
            return outcome
        last_error = "；".join(issues)
        hint = "\n".join(f"- {issue}" for issue in issues)
    if outcome is not None:
        return outcome  # 第二轮仍有软问题：收下，让 GET 详情提示
    raise BookOutputError(f"spine 生成失败：{last_error}")
