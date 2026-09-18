"use client";

/** 主题 hook：水合存储值 + 同步 <html data-theme> + 切换时落盘并同步后端设置。 */

import { useCallback, useEffect } from "react";
import { useThemeStore } from "@/i18n/theme-store";
import { resolveTheme, type Theme } from "@/lib/theme";
import { apiFetch } from "@/lib/api";

export function useTheme() {
  const theme = useThemeStore((s) => s.theme);
  const setTheme = useThemeStore((s) => s.setTheme);
  const hydrateFromStorage = useThemeStore((s) => s.hydrateFromStorage);

  useEffect(() => {
    hydrateFromStorage();
  }, [hydrateFromStorage]);

  useEffect(() => {
    const effective = resolveTheme(
      theme,
      window.matchMedia("(prefers-color-scheme: dark)").matches
    );
    document.documentElement.dataset.theme = effective;
  }, [theme]);

  const switchTheme = useCallback(
    (next: Theme) => {
      setTheme(next);
      // 同步后端 appearance 设置（失败不阻断本地切换）
      void apiFetch("/api/v1/settings/appearance", {
        method: "PUT",
        body: JSON.stringify({ values: { theme: next } }),
      }).catch(() => undefined);
    },
    [setTheme]
  );

  return { theme, switchTheme };
}
