"use client";

/** 界面语言 hook：t() 翻译 + setLang 切换（写 store 持久化 + 同步后端 appearance 设置）。 */

import { useCallback } from "react";
import { translate } from "@/i18n";
import { useLanguageStore } from "@/i18n/language-store";
import { apiFetch } from "@/lib/api";

export function useI18n() {
  const lang = useLanguageStore((s) => s.lang);
  const setLang = useLanguageStore((s) => s.setLang);

  const t = useCallback(
    (path: string, vars?: Record<string, string>) => translate(lang, path, vars),
    [lang]
  );

  const switchLanguage = useCallback(
    (next: typeof lang) => {
      if (next === lang) {
        return;
      }
      setLang(next);
      // 同步后端 appearance 设置（失败不阻断本地切换）
      void apiFetch("/api/v1/settings/appearance", {
        method: "PUT",
        body: JSON.stringify({ values: { ui_language: next } }),
      }).catch(() => undefined);
    },
    [lang, setLang]
  );

  return { t, lang, switchLanguage };
}
