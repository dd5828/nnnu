"use client";

/** 外观快捷面板：主题/语言即时切换（演示"改设置即时生效"闭环）。 */

import { useI18n } from "@/hooks/useI18n";
import { useMounted } from "@/hooks/useMounted";
import { useTheme } from "@/hooks/useTheme";
import { THEMES } from "@/lib/theme";

const THEME_LABEL_KEYS: Record<string, string> = {
  light: "common.themeLight",
  beige: "common.themeBeige",
  dark: "common.themeDark",
  glass: "common.themeGlass",
};

export default function AppearanceQuickPanel() {
  const { t, lang, switchLanguage } = useI18n();
  const { theme, switchTheme } = useTheme();
  const mounted = useMounted();

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-semibold">{t("settings.appearance.theme")}</h2>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-1.5">
          {THEMES.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => switchTheme(value)}
              className={`rounded-md px-2.5 py-1 ${
                theme === value
                  ? "bg-primary text-primary-foreground"
                  : "border border-border hover:bg-accent"
              }`}
            >
              {t(THEME_LABEL_KEYS[value])}
            </button>
          ))}
        </div>
        {mounted && (
          <div className="flex flex-wrap gap-1.5">
            {(["zh", "en"] as const).map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => switchLanguage(code)}
                className={`rounded-md px-2.5 py-1 ${
                  lang === code
                    ? "bg-primary text-primary-foreground"
                    : "border border-border hover:bg-accent"
                }`}
              >
                {code === "zh" ? "中文" : "English"}
              </button>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
