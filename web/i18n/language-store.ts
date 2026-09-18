/** 界面语言状态：Zustand + localStorage 持久化 + storage 事件跨标签页同步。 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { Language } from "./index";

export const LANGUAGE_STORAGE_KEY = "nnnu-language";

/** node（测试/SSR）环境下的无操作存储，避免访问不存在的 localStorage。 */
const noopStorage = createJSONStorage<LanguageState>(() => {
  if (typeof window !== "undefined") {
    return window.localStorage;
  }
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    removeItem: (key: string) => void map.delete(key),
  };
});

interface LanguageState {
  lang: Language;
  setLang: (lang: Language) => void;
}

export const useLanguageStore = create<LanguageState>()(
  persist(
    (set) => ({
      lang: "zh",
      setLang: (lang) => set({ lang }),
    }),
    { name: LANGUAGE_STORAGE_KEY, storage: noopStorage }
  )
);

// 跨标签页同步：其它标签页切语言时本页即时跟随
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key !== LANGUAGE_STORAGE_KEY || !event.newValue) {
      return;
    }
    try {
      const parsed = JSON.parse(event.newValue) as { state?: { lang?: Language } };
      if (parsed.state?.lang) {
        useLanguageStore.setState({ lang: parsed.state.lang });
      }
    } catch {
      // 非本应用写入的格式，忽略
    }
  });
}
