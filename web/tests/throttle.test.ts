import { afterEach, describe, expect, it, vi } from "vitest";
import { createThrottle } from "@/lib/throttle";

/** 流式正文章节用节流不用防抖：这条用例钉的就是「连续增量不被饿到流尾」的性质——
 * 换成尾部防抖（每次调用都顺延定时器），下面都会退化成「只在最后渲染一次」。 */
describe("createThrottle", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("首次调用立即执行，之后窗口内合并", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const throttle = createThrottle<[string]>((value) => seen.push(value), 100);

    throttle.call("a");
    expect(seen).toEqual(["a"]); // 首帧不等窗口

    throttle.call("b");
    throttle.call("c"); // 窗口内只留最新一份
    expect(seen).toEqual(["a"]);
    vi.advanceTimersByTime(100);
    expect(seen).toEqual(["a", "c"]);
  });

  it("增量间隔短于窗口时也持续渲染（防抖会一路顺延）", () => {
    vi.useFakeTimers();
    const seen: number[] = [];
    const throttle = createThrottle<[number]>((value) => seen.push(value), 90);

    // 每 30ms 一个增量，跑 900ms —— 模拟逐字流式
    for (let i = 1; i <= 30; i++) {
      throttle.call(i);
      vi.advanceTimersByTime(30);
    }

    // 10 秒窗口里最多允许 90ms 一次：30 个增量至少该渲染 8 次
    expect(seen.length).toBeGreaterThanOrEqual(8);
    // 且渲染的是沿途的中间态，不是「一动不动直到最后」
    expect(seen[0]).toBe(1);
    expect(new Set(seen).size).toBe(seen.length);
  });

  it("cancel 丢掉待执行的那一次", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const throttle = createThrottle<[string]>((value) => seen.push(value), 100);

    throttle.call("a");
    throttle.call("b");
    throttle.cancel();
    vi.advanceTimersByTime(500);
    expect(seen).toEqual(["a"]);
  });
});
