"use client";

/**
 * 子代理页（§7.12）：一页两标签——连接 / 历史导入。
 *
 * `?tab=` 深链供别处跳进来（消息上引用了某条导入会话时落到 `?tab=imports&id=imp-…`，
 * 见 `lib/citations.ts:refTarget`）；`?id=` 只在这一页里有意义，切标签时由
 * `AgentImportsPanel` 自己接管。读了 useSearchParams，所以本体包在 Suspense 里
 * （Next 16 的硬要求，照 /skills 的写法）。
 */

import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Bot, Loader2 } from "lucide-react";
import AgentImportsPanel from "@/components/agents/AgentImportsPanel";
import AgentPanel from "@/components/agents/AgentPanel";
import { useI18n } from "@/hooks/useI18n";

const TABS = [
  { value: "connections", key: "agents.tabConnections" },
  { value: "imports", key: "agents.tabImports" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

export default function AgentsPage() {
  return (
    <Suspense
      fallback={
        <main className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </main>
      }
    >
      <AgentsBoard />
    </Suspense>
  );
}

function AgentsBoard() {
  const { t } = useI18n();
  const router = useRouter();
  const searchParams = useSearchParams();
  const raw = searchParams.get("tab") ?? "";
  const tab: TabValue = TABS.some((item) => item.value === raw) ? (raw as TabValue) : "connections";
  const focusId = searchParams.get("id");

  const selectTab = (value: TabValue) => {
    // 切标签就丢掉 `?id=`：那是「历史导入」标签里的展开目标，带到别处没意义
    router.replace(value === "connections" ? "/agents" : `/agents?tab=${value}`);
  };

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div className="rounded-xl bg-accent p-2 text-primary">
            <Bot className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold">{t("nav.agents")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("agents.pageDesc")}</p>
          </div>
        </div>

        <div className="flex items-center gap-1 rounded-xl border border-border bg-surface p-1">
          {TABS.map((item) => (
            <button
              key={item.value}
              type="button"
              data-testid={`ag-tab-${item.value}`}
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

        {tab === "connections" ? <AgentPanel /> : <AgentImportsPanel focusId={focusId} />}
      </div>
    </main>
  );
}
