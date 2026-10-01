"use client";

/**
 * 记忆工作台（§7.10）：三层记忆的查看/编辑/删除 + 图谱卡片（证据链 / 语义知识图谱）
 * + 手动整合。
 *
 * - L3（画像）/ L2（分面事实）可编辑：改正文即进人工保护集，下轮整合不覆盖；
 * - L1（轨迹）只读：append-only，行号是引用锚点；
 * - 手动「立即整合」后台跑（202），overview 的 consolidating 置位时轮询到收尾。
 */

import { useState } from "react";
import { Brain, Loader2, Play, RefreshCw } from "lucide-react";
import ChipRow from "@/components/memory/Chips";
import GraphCard from "@/components/memory/GraphCard";
import MemoryEntryList from "@/components/memory/MemoryEntryList";
import MemoryL1 from "@/components/memory/MemoryL1";
import { useI18n } from "@/hooks/useI18n";
import { useConsolidateMemory, useMemoryDoc, useMemoryOverview } from "@/hooks/useMemory";
import { errorText } from "@/lib/errors";
import { formatBytes, formatTs, l3DocLabelKey, surfaceLabelKey } from "@/lib/memory";

type Tab = "l3" | "l2" | "l1";

const TABS: Tab[] = ["l3", "l2", "l1"];

export default function MemoryPage() {
  const { t } = useI18n();
  const overview = useMemoryOverview();
  const [tab, setTab] = useState<Tab>("l3");
  const [l3Doc, setL3Doc] = useState<string | null>(null);
  const [l2Surface, setL2Surface] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const surfaces = overview.data?.surfaces ?? [];
  const l3Docs = overview.data?.l3_docs ?? [];

  // 首选项兜底：用户还没点过时用第一项（派生值，不用 effect 回写 state）
  const activeL3 = l3Doc ?? l3Docs[0] ?? null;
  const activeL2 = l2Surface ?? surfaces[0] ?? null;

  const l3 = useMemoryDoc("l3", tab === "l3" ? activeL3 : null);
  const l2 = useMemoryDoc("l2", tab === "l2" ? activeL2 : null);
  const consolidate = useConsolidateMemory();

  const data = overview.data;
  const l1Lines = data ? Object.values(data.l1).reduce((sum, item) => sum + item.lines, 0) : 0;
  const l2Entries = data ? Object.values(data.l2).reduce((sum, item) => sum + item.entries, 0) : 0;
  const l3Entries = data ? Object.values(data.l3).reduce((sum, item) => sum + item.entries, 0) : 0;
  const consolidating = Boolean(data?.consolidating) || consolidate.isPending;

  const handleConsolidate = async () => {
    setActionError(null);
    try {
      await consolidate.mutateAsync();
    } catch (err) {
      setActionError(errorText(err, t("common.requestFailed")));
    }
  };

  const l3DocView = l3.data?.docs[0];
  const l2DocView = l2.data?.docs[0];

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-4 px-4 py-6">
        {/* 页头：标题 + 手动整合 */}
        <div className="flex flex-wrap items-start gap-3">
          <div className="rounded-xl bg-accent p-2 text-primary">
            <Brain className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold">{t("memory.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("memory.subtitle")}</p>
          </div>
          <button
            type="button"
            data-testid="memory-consolidate"
            disabled={consolidating || !data}
            onClick={() => void handleConsolidate()}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            {consolidating ? (
              <>
                <Loader2 className="size-4 animate-spin" />
                {t("memory.consolidating")}
              </>
            ) : (
              <>
                <Play className="size-4" />
                {t("memory.consolidateNow")}
              </>
            )}
          </button>
        </div>

        {overview.isLoading ? (
          <div className="flex justify-center py-12">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : overview.error ? (
          <p className="text-sm text-danger">
            {errorText(overview.error, t("common.requestFailed"))}
          </p>
        ) : (
          data && (
            <>
              {actionError && <p className="text-sm text-danger">{actionError}</p>}

              {/* 概览统计与整合状态 */}
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {(
                  [
                    {
                      testId: "memory-stat-l1",
                      label: t("memory.statL1"),
                      value: t("memory.statLines", { n: String(l1Lines) }),
                    },
                    {
                      testId: "memory-stat-l2",
                      label: t("memory.statL2"),
                      value: t("memory.statEntries", { n: String(l2Entries) }),
                    },
                    {
                      testId: "memory-stat-l3",
                      label: t("memory.statL3"),
                      value: t("memory.statEntries", { n: String(l3Entries) }),
                    },
                    {
                      testId: "memory-stat-pending",
                      label: t("memory.statPending"),
                      value: t("memory.statTurns", { n: String(data.turns_since_consolidation) }),
                    },
                  ] as const
                ).map((item) => (
                  <div
                    key={item.label}
                    data-testid={item.testId}
                    className="rounded-xl border border-border bg-surface px-3 py-2"
                  >
                    <div className="text-[11px] text-muted">{item.label}</div>
                    <div className="mt-0.5 text-sm font-medium">{item.value}</div>
                  </div>
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                <span data-testid="memory-last-run">
                  {data.last_run
                    ? t("memory.lastRun", {
                        status: t(`memory.run.${data.last_run.status}`),
                        time: formatTs(data.last_run.finished_at ?? data.last_run.started_at),
                      })
                    : t("memory.neverRun")}
                </span>
                <span>
                  {t("memory.autoHint", {
                    threshold: String(data.config.auto_threshold_turns),
                    state: data.config.auto_enabled ? t("memory.autoOn") : t("memory.autoOff"),
                  })}
                </span>
              </div>

              {/* 三层浏览器 */}
              <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
                <div className="flex flex-wrap items-center gap-2">
                  {TABS.map((value) => (
                    <button
                      key={value}
                      type="button"
                      data-testid={`memory-tab-${value}`}
                      aria-pressed={tab === value}
                      onClick={() => setTab(value)}
                      className={`rounded-md px-3 py-1.5 text-sm ${
                        tab === value ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                      }`}
                    >
                      {t(`memory.layer.${value}`)}
                    </button>
                  ))}
                </div>

                {tab === "l3" && (
                  <div className="space-y-3">
                    <ChipRow
                      testId="l3-docs"
                      items={l3Docs.map((doc) => ({
                        value: doc,
                        label: t(l3DocLabelKey(doc)),
                        count: data.l3[doc]?.entries ?? 0,
                      }))}
                      active={activeL3}
                      onSelect={setL3Doc}
                    />
                    {l3.isLoading ? (
                      <div className="flex justify-center py-8">
                        <Loader2 className="h-4 w-4 animate-spin text-muted" />
                      </div>
                    ) : l3.error ? (
                      <p className="text-sm text-danger">
                        {errorText(l3.error, t("common.requestFailed"))}
                      </p>
                    ) : (
                      l3DocView && <MemoryEntryList layer="l3" doc={l3DocView} />
                    )}
                  </div>
                )}

                {tab === "l2" && (
                  <div className="space-y-3">
                    <ChipRow
                      testId="l2-surfaces"
                      items={surfaces.map((surface) => ({
                        value: surface,
                        label: t(surfaceLabelKey(surface)),
                        count: data.l2[surface]?.entries ?? 0,
                      }))}
                      active={activeL2}
                      onSelect={setL2Surface}
                    />
                    {l2.isLoading ? (
                      <div className="flex justify-center py-8">
                        <Loader2 className="h-4 w-4 animate-spin text-muted" />
                      </div>
                    ) : l2.error ? (
                      <p className="text-sm text-danger">
                        {errorText(l2.error, t("common.requestFailed"))}
                      </p>
                    ) : (
                      l2DocView && <MemoryEntryList layer="l2" doc={l2DocView} />
                    )}
                  </div>
                )}

                {tab === "l1" && <MemoryL1 surfaces={surfaces} />}
              </section>

              {/* 图谱卡片：证据链 / 语义知识图谱两种模式（外壳与模式状态在组件里） */}
              <GraphCard />

              {/* 分面水位（低频信息，收在最后） */}
              <details className="rounded-xl border border-border bg-surface px-4 py-3 text-xs text-muted">
                <summary className="cursor-pointer">{t("memory.watermarksTitle")}</summary>
                <ul className="mt-2 space-y-1">
                  {data.surfaces.map((surface) => {
                    const mark = data.watermarks[surface] ?? { file: "", line: 0 };
                    const l1 = data.l1[surface];
                    return (
                      <li key={surface} className="flex flex-wrap items-center gap-2">
                        <span className="w-24 shrink-0">{t(surfaceLabelKey(surface))}</span>
                        <span>
                          {t("memory.watermark", {
                            file: mark.file || "—",
                            line: String(mark.line),
                          })}
                        </span>
                        <span className="flex-1" />
                        <span>
                          {l1
                            ? t("memory.surfaceStats", {
                                lines: String(l1.lines),
                                size: formatBytes(l1.bytes),
                              })
                            : ""}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </details>

              <p className="flex items-center gap-1.5 text-xs text-muted">
                <RefreshCw className="size-3" />
                {t("memory.refreshHint")}
              </p>
            </>
          )
        )}
      </div>
    </main>
  );
}
