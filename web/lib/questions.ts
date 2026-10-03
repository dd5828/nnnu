/** 题库增强的纯计算（P9）：错因枚举、标签解析、草稿转提交体、相似度百分比。
 *
 * 全部是纯函数，交给 `tests/questions.test.ts` 盯（vitest 是 node 环境，
 * 只收 tests 目录下的 .test.ts，组件测试在本仓跑不起来）。
 */

import type { QuestionInput } from "@/hooks/useQuestions";
import type { ErrorCause, VariantDraft } from "@/types/api";

/** 错因枚举（顺序即界面 chips 顺序；键与后端 models.ERROR_CAUSES 对齐）。 */
export const ERROR_CAUSES: ErrorCause[] = [
  "concept_unclear",
  "misread",
  "calculation",
  "method_missing",
  "memory_weak",
];

/** 错因上限（与后端 service.MAX_ERROR_CAUSES 一致）。 */
export const MAX_ERROR_CAUSES = 3;

/** 选项标签：位置推 A/B/C/D（与后端存法一致）。 */
export function optionLabel(index: number): string {
  return String.fromCharCode(65 + index);
}

/** 错因键 → 展示文案键（locale questions.cause_*）。 */
export function errorCauseKey(cause: ErrorCause): string {
  return `questions.cause_${cause}`;
}

/** 标签输入（逗号/顿号/换行分隔）→ 去重保序的标签数组（空串丢，上限 10 与后端一致）。 */
export function parseTagsInput(text: string): string[] {
  const tags: string[] = [];
  for (const piece of text.split(/[,，、\n]+/)) {
    const tag = piece.trim();
    if (tag && !tags.includes(tag)) {
      tags.push(tag);
    }
    if (tags.length >= 10) {
      break;
    }
  }
  return tags;
}

/** 标签数组 → 输入框文本（表单回填用）。 */
export function tagsToInput(tags: string[]): string {
  return tags.join(", ");
}

/** 变式题草稿 → 批量采纳提交体：白名单取字段，剔掉 duplicate_score 这类预览元数据。 */
export function draftToAdoptInput(draft: VariantDraft): QuestionInput {
  return {
    stem: draft.stem,
    type: draft.type,
    options: draft.options,
    answer: draft.answer,
    explanation: draft.explanation || null,
    knowledge_point: draft.knowledge_point,
    difficulty: draft.difficulty,
    tags: draft.tags,
  };
}

/** Jaccard 相似度（0–1）→ 展示用百分比整数；坏数不画鬼环。 */
export function similarityPercent(score: number): number {
  if (!Number.isFinite(score)) {
    return 0;
  }
  return Math.round(Math.min(1, Math.max(0, score)) * 100);
}
