import { describe, expect, it } from "vitest";
import { TURN_TOP_GAP_PX, tailSpacerHeight } from "@/lib/chat-scroll";

describe("尾巴占位高度", () => {
  it("内容比一屏短：补到「回合顶 - 留白」恰好可滚", () => {
    // 视口 800、回合顶以下 200 → 需要 800 - 8 - 200 = 592 的占位
    expect(tailSpacerHeight(800, 200)).toBe(800 - TURN_TOP_GAP_PX - 200);
  });

  it("内容长过一屏：不占位（0）", () => {
    expect(tailSpacerHeight(800, 800)).toBe(0);
    expect(tailSpacerHeight(800, 1200)).toBe(0);
  });

  it("边界：刚好一屏也归零（可达性已由内容本身满足）", () => {
    expect(tailSpacerHeight(800, 800 - TURN_TOP_GAP_PX)).toBe(0);
    expect(tailSpacerHeight(800, 800 - TURN_TOP_GAP_PX - 1)).toBe(1);
  });

  it("不动负值：内容再多也是 0，不产生负高", () => {
    expect(tailSpacerHeight(0, 100)).toBe(0);
  });
});
