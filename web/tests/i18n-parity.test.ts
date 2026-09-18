import { describe, expect, it } from "vitest";
import { translate } from "@/i18n";
import en from "@/locales/en.json";
import zh from "@/locales/zh.json";

/** 递归展开字典键路径集合。 */
function allKeys(obj: unknown, prefix = ""): Set<string> {
  const keys = new Set<string>();
  if (typeof obj !== "object" || obj === null) {
    return keys;
  }
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const full = `${prefix}${key}`;
    keys.add(full);
    for (const sub of allKeys(value, `${full}.`)) {
      keys.add(sub);
    }
  }
  return keys;
}

describe("locales 键集合", () => {
  it("en/zh 键集合完全一致", () => {
    const enKeys = allKeys(en);
    const zhKeys = allKeys(zh);
    expect([...enKeys].filter((k) => !zhKeys.has(k))).toEqual([]);
    expect([...zhKeys].filter((k) => !enKeys.has(k))).toEqual([]);
  });
});

describe("translate", () => {
  it("命中目标语言", () => {
    expect(translate("zh", "common.tagline")).toBe("AI 学习工作台");
    expect(translate("en", "common.tagline")).toBe("AI learning workbench");
  });

  it("zh 缺键回退英文", () => {
    expect(translate("zh", "health.seconds", { n: "3" })).toBe("3 秒");
  });

  it("缺键返回键名本身", () => {
    expect(translate("zh", "nav.nope")).toBe("nav.nope");
  });

  it("支持 {var} 插值", () => {
    expect(translate("en", "health.seconds", { n: "5" })).toBe("5s");
  });
});
