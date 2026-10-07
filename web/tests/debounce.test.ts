import { afterEach, describe, expect, it, vi } from "vitest";
import { createDebounce } from "@/lib/debounce";

/** Co-Writer 自动保存用的尾部防抖：打字期间一个请求都不发，停手才执行一次；
 *  flush 是「切页/发起改写前先落盘」的出口，cancel 是「accept 写回前别让旧内容冲掉」的出口。 */
describe("createDebounce", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("窗口内连续调用只顺延，不执行；安静满窗口才执行最后一次", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const debounce = createDebounce<[string]>((value) => seen.push(value), 2000);

    debounce.call("a");
    expect(seen).toEqual([]); // 与节流相反：不立即执行
    vi.advanceTimersByTime(1500);
    debounce.call("b"); // 重新计时
    vi.advanceTimersByTime(1500);
    expect(seen).toEqual([]); // a 被顺延掉了
    vi.advanceTimersByTime(500);
    expect(seen).toEqual(["b"]);
  });

  it("flush 立刻执行挂起的那一次", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const debounce = createDebounce<[string]>((value) => seen.push(value), 2000);

    debounce.call("草稿");
    debounce.flush();
    expect(seen).toEqual(["草稿"]);
    vi.advanceTimersByTime(5000); // flush 后到点不再重复执行
    expect(seen).toEqual(["草稿"]);
  });

  it("flush 无欠账时什么都不做", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const debounce = createDebounce<[string]>((value) => seen.push(value), 2000);

    debounce.flush();
    expect(seen).toEqual([]);
  });

  it("cancel 丢弃挂起的那一次", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const debounce = createDebounce<[string]>((value) => seen.push(value), 2000);

    debounce.call("旧内容");
    debounce.cancel();
    vi.advanceTimersByTime(5000);
    expect(seen).toEqual([]);
    debounce.flush(); // cancel 后 flush 也不会把它捞回来
    expect(seen).toEqual([]);
  });
});
