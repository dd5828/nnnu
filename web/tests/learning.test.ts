import { describe, expect, it } from "vitest";
import {
  clearedOf,
  gateKey,
  indentPx,
  masteryPercent,
  nextActionKey,
  nodeHref,
  nodeStateKey,
  nodeTypeKey,
  pathHref,
  reviewWhen,
  ringTone,
  strategyKey,
  sweepDegrees,
  weakPointHref,
} from "@/lib/learning";
import type { LearningNode } from "@/types/api";

/** 拼一个够用的节点（只看 lib 用到的字段）。 */
function node(overrides: Partial<LearningNode> = {}): LearningNode {
  return {
    id: "lnode-1",
    path_id: "lpath-1",
    parent_id: null,
    title: "向量与线性组合",
    node_type: "concept",
    description: "",
    depth: 0,
    sort_order: 0,
    mastery: 0,
    state: "not_started",
    review_stage: 0,
    next_review_at: null,
    last_practiced_at: null,
    assess_passed: false,
    assessed_at: null,
    gate: null,
    gate_kind: "qualitative",
    cleared: false,
    due: false,
    created_at: 0,
    updated_at: 0,
    ...overrides,
  };
}

describe("圆环角度", () => {
  it("0–100 折算 0–360 度", () => {
    expect(sweepDegrees(0)).toBe(0);
    expect(sweepDegrees(50)).toBe(180);
    expect(sweepDegrees(100)).toBe(360);
  });

  it("越界与坏数夹住，不画鬼环", () => {
    expect(sweepDegrees(-10)).toBe(0);
    expect(sweepDegrees(120)).toBe(360);
    expect(sweepDegrees(Number.NaN)).toBe(0);
  });

  it("角度与门刻度同尺：门在环上的位置就是 gate 的角度", () => {
    expect(sweepDegrees(90)).toBe(324);
  });
});

describe("圆环配色", () => {
  it("先判 cleared：到期该复习的节点仍然是学会了，画绿不画红", () => {
    expect(ringTone(true, "reviewing")).toBe("text-success");
    expect(ringTone(true, "mastered")).toBe("text-success");
  });

  it("没过门才看状态：待复习红、在学蓝、没开始灰", () => {
    expect(ringTone(false, "reviewing")).toBe("text-danger");
    expect(ringTone(false, "learning")).toBe("text-primary");
    expect(ringTone(false, "not_started")).toBe("text-muted");
  });
});

describe("是否过门", () => {
  it("只读服务端给的 cleared（定性节点没有数字门，本地比不了）", () => {
    expect(clearedOf(node({ cleared: true }))).toBe(true);
    expect(clearedOf(node({ cleared: false, mastery: 100 }))).toBe(false);
  });
});

describe("门与建议动作的文案键", () => {
  it("定量给分数线、定性说「讲一遍」", () => {
    expect(gateKey(node({ gate: 90, gate_kind: "quantitative" }))).toEqual({
      key: "learning.gateQuantitative",
      vars: { gate: "90" },
    });
    expect(gateKey(node({ gate: null, gate_kind: "qualitative" }))).toEqual({
      key: "learning.gateQualitative",
      vars: {},
    });
  });

  it("分数取整（后端是浮点门）", () => {
    expect(gateKey(node({ gate: 89.999, gate_kind: "quantitative" })).vars.gate).toBe("90");
  });

  it("动作键一一对应，缺省落到 complete", () => {
    expect(nextActionKey("probe")).toBe("learning.action_probe");
    expect(nextActionKey("answer_pending")).toBe("learning.action_answer_pending");
    expect(nextActionKey(null)).toBe("learning.action_complete");
    expect(nextActionKey(undefined)).toBe("learning.action_complete");
    expect(nextActionKey("")).toBe("learning.action_complete");
  });
});

describe("树缩进与高亮", () => {
  it("每深一层 16px，负深度按 0 算", () => {
    expect([indentPx(0), indentPx(1), indentPx(3)]).toEqual([0, 16, 48]);
    expect(indentPx(-2)).toBe(0);
  });
});

describe("深链", () => {
  it("薄弱点跳到题库页并带上 node 过滤", () => {
    expect(weakPointHref("lnode-9")).toBe("/questions?node=lnode-9");
  });

  it("路径与节点地址（node 是 query，可分享）", () => {
    expect(pathHref("lpath-1")).toBe("/learning/lpath-1");
    expect(nodeHref("lpath-1", "lnode-2")).toBe("/learning/lpath-1?node=lnode-2");
  });
});

describe("复习时间", () => {
  const now = 1_700_000_000;

  it("没排 → 暂无；已到期 → 现在就该复习", () => {
    expect(reviewWhen(null, now).key).toBe("learning.reviewNone");
    expect(reviewWhen(now - 60, now).key).toBe("learning.reviewDue");
  });

  it("未来按天向上取整（不到一天也算 1 天）", () => {
    expect(reviewWhen(now + 3600, now)).toMatchObject({
      key: "learning.reviewInDays",
      vars: { n: "1" },
    });
    expect(reviewWhen(now + 86400 * 2.5, now).vars.n).toBe("3");
  });
});

describe("文案键与百分比", () => {
  it("类型/状态/策略都落在 learning.* 命名空间（四类型）", () => {
    expect(nodeTypeKey("memory")).toBe("learning.type_memory");
    expect(nodeTypeKey("design")).toBe("learning.type_design");
    expect(nodeStateKey("reviewing")).toBe("learning.state_reviewing");
    expect(strategyKey("procedure")).toBe("learning.strategy_procedure");
  });

  it("百分比取整并夹在 0–100", () => {
    expect(masteryPercent(86.3)).toBe(86);
    expect(masteryPercent(-3)).toBe(0);
    expect(masteryPercent(120)).toBe(100);
    expect(masteryPercent(Number.NaN)).toBe(0);
  });
});
