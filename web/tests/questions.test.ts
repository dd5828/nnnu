import { describe, expect, it } from "vitest";
import {
  ERROR_CAUSES,
  draftToAdoptInput,
  errorCauseKey,
  optionLabel,
  parseTagsInput,
  similarityPercent,
  tagsToInput,
} from "@/lib/questions";
import type { VariantDraft } from "@/types/api";

/** 拼一道够用的草稿（只看 draftToAdoptInput 用到的字段）。 */
function draft(overrides: Partial<VariantDraft> = {}): VariantDraft {
  return {
    stem: "求 f(x)=x² 在 x=3 处的导数",
    options: ["6", "9", "3", "12"],
    answer: "A",
    explanation: "幂法则",
    type: "single",
    difficulty: "medium",
    knowledge_point: "导数",
    tags: ["变式"],
    duplicate_score: 0.9565,
    ...overrides,
  };
}

describe("错因枚举", () => {
  it("五个键不重复，与后端 models.ERROR_CAUSES 对齐", () => {
    expect(ERROR_CAUSES).toEqual([
      "concept_unclear",
      "misread",
      "calculation",
      "method_missing",
      "memory_weak",
    ]);
    expect(new Set(ERROR_CAUSES).size).toBe(ERROR_CAUSES.length);
  });

  it("键 → locale 文案键", () => {
    expect(errorCauseKey("calculation")).toBe("questions.cause_calculation");
  });
});

describe("选项标签", () => {
  it("位置推 A/B/C/D", () => {
    expect(optionLabel(0)).toBe("A");
    expect(optionLabel(3)).toBe("D");
  });
});

describe("标签输入", () => {
  it("逗号/顿号/换行都能切，去重保序", () => {
    expect(parseTagsInput("常考, 易错、常考\n高频")).toEqual(["常考", "易错", "高频"]);
  });

  it("空串与空白丢干净", () => {
    expect(parseTagsInput("  ,  、\n ")).toEqual([]);
  });

  it("超过 10 个截断（与后端上限一致）", () => {
    const many = Array.from({ length: 15 }, (_, index) => `t${index}`).join(",");
    expect(parseTagsInput(many)).toHaveLength(10);
  });

  it("数组 ↔ 输入框文本往返", () => {
    expect(tagsToInput(["常考", "易错"])).toBe("常考, 易错");
    expect(parseTagsInput(tagsToInput(["常考", "易错"]))).toEqual(["常考", "易错"]);
  });
});

describe("草稿转提交体", () => {
  it("白名单取字段，剔掉 duplicate_score", () => {
    const input = draftToAdoptInput(draft());
    expect(input).toEqual({
      stem: "求 f(x)=x² 在 x=3 处的导数",
      type: "single",
      options: ["6", "9", "3", "12"],
      answer: "A",
      explanation: "幂法则",
      knowledge_point: "导数",
      difficulty: "medium",
      tags: ["变式"],
    });
    expect("duplicate_score" in input).toBe(false);
  });

  it("空解析转 null（后端可留空的字段）", () => {
    expect(draftToAdoptInput(draft({ explanation: "" })).explanation).toBeNull();
  });
});

describe("相似度百分比", () => {
  it("0–1 折算整数百分比", () => {
    expect(similarityPercent(0.9565)).toBe(96);
    expect(similarityPercent(0)).toBe(0);
    expect(similarityPercent(1)).toBe(100);
  });

  it("越界与坏数夹住，不画鬼环", () => {
    expect(similarityPercent(-0.2)).toBe(0);
    expect(similarityPercent(1.5)).toBe(100);
    expect(similarityPercent(Number.NaN)).toBe(0);
  });
});
