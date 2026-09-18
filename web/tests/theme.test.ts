import { describe, expect, it } from "vitest";
import { isTheme, resolveTheme, THEMES } from "@/lib/theme";

describe("isTheme", () => {
  it("合法主题通过", () => {
    for (const theme of THEMES) {
      expect(isTheme(theme)).toBe(true);
    }
  });

  it("非法值拒绝", () => {
    expect(isTheme("neon")).toBe(false);
    expect(isTheme(null)).toBe(false);
    expect(isTheme(undefined)).toBe(false);
    expect(isTheme(42)).toBe(false);
  });
});

describe("resolveTheme", () => {
  it("存储值为合法主题时直接采用", () => {
    expect(resolveTheme("beige", false)).toBe("beige");
    expect(resolveTheme("glass", true)).toBe("glass");
  });

  it("非法或缺失时回退系统偏好", () => {
    expect(resolveTheme("neon", true)).toBe("dark");
    expect(resolveTheme(null, false)).toBe("light");
    expect(resolveTheme(null, true)).toBe("dark");
  });
});
