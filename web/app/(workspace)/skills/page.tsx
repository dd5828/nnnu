"use client";

/**
 * 技能页（§7.16 / §7.17 / §7.18）：一页三标签——技能 / MCP / CLI 应用。
 *
 * MCP 与 CLI 两个标签在刀三、刀四补上（P11 分五刀实施），这一刀先把「技能」和
 * 「市场」交付，标签位先占好，免得回头再改导航结构。
 *
 * `?tab=` 深链：从别处跳过来能直接落到某个标签，刷新也不丢。读了 useSearchParams，
 * 所以本体包在 Suspense 里（Next 16 的硬要求）。
 */

import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, Sparkles } from "lucide-react";
import SkillHub from "@/components/skills/SkillHub";
import SkillPanel from "@/components/skills/SkillPanel";
import { useI18n } from "@/hooks/useI18n";

const TABS = [
  { value: "skills", key: "skills.tabSkills" },
  { value: "hub", key: "skills.tabHub" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

export default function SkillsPage() {
  return (
    <Suspense
      fallback={
        <main className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </main>
      }
    >
      <SkillsBoard />
    </Suspense>
  );
}

function SkillsBoard() {
  const { t } = useI18n();
  const router = useRouter();
  const searchParams = useSearchParams();
  const raw = searchParams.get("tab") ?? "";
  const tab: TabValue = TABS.some((item) => item.value === raw) ? (raw as TabValue) : "skills";

  const selectTab = (value: TabValue) => {
    router.replace(value === "skills" ? "/skills" : `/skills?tab=${value}`);
  };

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div className="rounded-xl bg-accent p-2 text-primary">
            <Sparkles className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold">{t("nav.skills")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("skills.pageDesc")}</p>
          </div>
        </div>

        <div className="flex items-center gap-1 rounded-xl border border-border bg-surface p-1">
          {TABS.map((item) => (
            <button
              key={item.value}
              type="button"
              data-testid={`sk-tab-${item.value}`}
              onClick={() => selectTab(item.value)}
              className={`flex-1 rounded-lg px-3 py-1.5 text-sm transition-colors ${
                tab === item.value
                  ? "bg-accent font-medium text-foreground"
                  : "text-muted hover:bg-accent/60"
              }`}
            >
              {t(item.key)}
            </button>
          ))}
        </div>

        {tab === "skills" ? <SkillPanel /> : <SkillHub />}
      </div>
    </main>
  );
}
