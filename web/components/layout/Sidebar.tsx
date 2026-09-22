"use client";

/** 侧边栏空壳：§7.21 全部工作区入口（P0 仅首页可用）+ 语言/主题切换器。 */

import Link from "next/link";
import {
  BookOpen,
  Bot,
  Brain,
  Database,
  FlaskConical,
  GraduationCap,
  Home,
  ListChecks,
  MessageSquare,
  NotebookPen,
  PenLine,
  Settings,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useMounted } from "@/hooks/useMounted";
import { useTheme } from "@/hooks/useTheme";
import { THEMES } from "@/lib/theme";

interface NavItem {
  href: string;
  key: string;
  icon: LucideIcon;
  enabled?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { href: "/", key: "nav.home", icon: Home, enabled: true },
  { href: "/book", key: "nav.book", icon: BookOpen },
  { href: "/co-writer", key: "nav.coWriter", icon: PenLine },
  { href: "/knowledge", key: "nav.knowledge", icon: Database },
  { href: "/learning", key: "nav.learning", icon: GraduationCap },
  { href: "/memory", key: "nav.memory", icon: Brain },
  { href: "/notebooks", key: "nav.notebooks", icon: NotebookPen },
  { href: "/questions", key: "nav.questions", icon: ListChecks },
  { href: "/skills", key: "nav.skills", icon: Sparkles },
  { href: "/partners", key: "nav.partners", icon: MessageSquare },
  { href: "/agents", key: "nav.agents", icon: Bot },
  { href: "/playground", key: "nav.playground", icon: FlaskConical },
  { href: "/settings", key: "nav.settings", icon: Settings, enabled: true },
];

const THEME_LABEL_KEYS: Record<string, string> = {
  light: "common.themeLight",
  beige: "common.themeBeige",
  dark: "common.themeDark",
  glass: "common.themeGlass",
};

export default function Sidebar() {
  const { t, lang, switchLanguage } = useI18n();
  const { theme, switchTheme } = useTheme();
  const mounted = useMounted();

  return (
    <aside className="flex w-52 shrink-0 flex-col border-r border-border bg-sidebar-bg text-sidebar-fg">
      <div className="flex items-center gap-2 px-4 py-4">
        <span className="text-lg font-semibold">{t("common.appName")}</span>
      </div>

      <nav className="flex-1 space-y-0.5 overflow-y-auto px-2">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const label = t(item.key);
          if (item.enabled) {
            return (
              <Link
                key={item.href}
                href={item.href}
                className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm hover:bg-accent"
              >
                <Icon className="size-4" />
                {label}
              </Link>
            );
          }
          return (
            <button
              key={item.href}
              type="button"
              disabled
              title={`${label} · ${t("common.comingSoon")}`}
              className="flex w-full cursor-not-allowed items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm opacity-50"
            >
              <Icon className="size-4" />
              <span className="flex-1">{label}</span>
              <span className="rounded bg-accent px-1.5 py-0.5 text-[10px]">
                {t("common.comingSoon")}
              </span>
            </button>
          );
        })}
      </nav>

      {mounted && (
        <div className="space-y-3 border-t border-border px-3 py-3 text-xs">
          <div>
            <div className="mb-1 opacity-70">{t("common.language")}</div>
            <div className="flex gap-1">
              {(["zh", "en"] as const).map((code) => (
                <button
                  key={code}
                  type="button"
                  onClick={() => switchLanguage(code)}
                  className={`rounded px-2 py-1 ${
                    lang === code ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                  }`}
                >
                  {code === "zh" ? "中文" : "EN"}
                </button>
              ))}
            </div>
          </div>
          <div>
            <div className="mb-1 opacity-70">{t("common.theme")}</div>
            <div className="flex flex-wrap gap-1">
              {THEMES.map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => switchTheme(value)}
                  className={`rounded px-2 py-1 ${
                    theme === value ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                  }`}
                >
                  {t(THEME_LABEL_KEYS[value])}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
