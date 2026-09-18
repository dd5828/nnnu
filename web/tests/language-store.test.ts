import { describe, expect, it } from "vitest";
import { useLanguageStore } from "@/i18n/language-store";

describe("language store", () => {
  it("默认语言为 zh", () => {
    expect(useLanguageStore.getState().lang).toBe("zh");
  });

  it("setLang 更新状态（node 环境走无操作存储，不抛异常）", () => {
    useLanguageStore.getState().setLang("en");
    expect(useLanguageStore.getState().lang).toBe("en");
    useLanguageStore.getState().setLang("zh");
    expect(useLanguageStore.getState().lang).toBe("zh");
  });
});
