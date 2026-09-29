/** 能力清单（§6.4/§7.6）的纯函数部分：能力 ↔ 阶段、笔记本记录类型、大纲判定。
 *
 * 组件测在 node 环境跑不了（P5 批三的结论），所以协议性的判断一律抽成纯函数放这儿测。
 */

import { describe, expect, it } from "vitest";
import {
  CAPABILITY_MASTERY,
  CAPABILITY_QUESTION,
  CAPABILITY_RESEARCH,
  CAPABILITY_SOLVE,
  RESEARCH_DEPTHS,
  RESEARCH_MODES,
  STAGES_BY_CAPABILITY,
  STAGE_LABEL_KEYS,
  isResearchOutline,
  recordTypeFor,
} from "@/lib/capabilities";

describe("阶段清单", () => {
  it("深度研究是四阶段，且有中文文案键", () => {
    expect(STAGES_BY_CAPABILITY[CAPABILITY_RESEARCH]).toEqual([
      "rephrasing",
      "decomposing",
      "researching",
      "reporting",
    ]);
    for (const stage of STAGES_BY_CAPABILITY[CAPABILITY_RESEARCH]) {
      expect(STAGE_LABEL_KEYS[stage]).toBeTruthy();
    }
  });

  it("档位与模式取值与后端同表，默认值在表里", () => {
    expect([...RESEARCH_DEPTHS]).toEqual(["quick", "standard", "deep"]);
    expect([...RESEARCH_MODES]).toEqual(["report", "answer"]);
    expect(RESEARCH_DEPTHS).toContain("standard");
    expect(RESEARCH_MODES).toContain("report");
  });
});

describe("大纲判定", () => {
  it("研究大纲正文（含修订版）都算停在大纲上", () => {
    expect(isResearchOutline("## 研究大纲\n\n**研究主题**：…")).toBe(true);
    expect(isResearchOutline("## 研究大纲（已按你的意见调整）\n\n…")).toBe(true);
    expect(isResearchOutline("## Research outline\n\n**Topic**: …")).toBe(true);
  });

  it("报告正文与其它能力的正文都不算", () => {
    expect(isResearchOutline("## 研究报告\n\n综合结论：…")).toBe(false);
    expect(isResearchOutline("## Research report\n\n…")).toBe(false);
    expect(isResearchOutline("## 解题规划\n\n…")).toBe(false);
    expect(isResearchOutline("")).toBe(false);
  });
});

describe("笔记本记录类型", () => {
  it("四个能力各归各的类型，其余算 chat", () => {
    expect(recordTypeFor(CAPABILITY_SOLVE)).toBe("solve");
    expect(recordTypeFor(CAPABILITY_QUESTION)).toBe("question");
    expect(recordTypeFor(CAPABILITY_RESEARCH)).toBe("research");
    expect(recordTypeFor(CAPABILITY_MASTERY)).toBe("chat");
    expect(recordTypeFor("chat")).toBe("chat");
  });
});
