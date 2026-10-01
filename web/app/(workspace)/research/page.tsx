"use client";

/**
 * 调研历史（§7.6「我的历次调研」）：历次深度研究一眼看全，点进去看大纲与报告。
 *
 * 只读页——调研在聊天页发起，这里只翻已经落库的账（报告正文不存库，详情端点现查，
 * 见 `api/routers/research.py`）。旧的在跑、新的在前，跟会话列表一个方向。
 */

import Link from "next/link";
import { Loader2, Telescope } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useResearchRuns } from "@/hooks/useResearch";
import { errorText } from "@/lib/errors";
import type { ResearchRunStatus } from "@/types/api";

const STATUS_KEYS: Record<ResearchRunStatus, string> = {
  confirming: "research.statusConfirming",
  researching: "research.statusResearching",
  reported: "research.statusReported",
  partial: "research.statusPartial",
  abandoned: "research.statusAbandoned",
};

const STATUS_CLASS: Record<ResearchRunStatus, string> = {
  confirming: "bg-accent text-muted",
  researching: "bg-accent text-primary",
  reported: "bg-accent text-success",
  partial: "bg-accent text-danger",
  abandoned: "bg-accent text-muted",
};

function dateLabel(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

export default function ResearchPage() {
  const { t } = useI18n();
  const { data, isLoading, error } = useResearchRuns();
  const runs = data?.runs ?? [];

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div className="rounded-xl bg-accent p-2 text-primary">
            <Telescope className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold">{t("research.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("research.subtitle")}</p>
          </div>
          {runs.length > 0 && (
            <span className="shrink-0 text-xs text-muted">
              {t("research.count", { n: String(data?.total ?? runs.length) })}
            </span>
          )}
        </div>

        {isLoading ? (
          <div className="flex justify-center py-10">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
        ) : runs.length === 0 ? (
          <p
            data-testid="research-empty"
            className="rounded-xl border border-dashed border-border py-10 text-center text-sm text-muted"
          >
            {t("research.empty")}
          </p>
        ) : (
          <ul className="space-y-2">
            {runs.map((run) => (
              <li key={run.id}>
                <Link
                  href={`/research/${run.id}`}
                  data-testid="research-run"
                  className="block rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40"
                >
                  <div className="flex items-start gap-2">
                    <span className="min-w-0 flex-1 break-words text-sm font-medium">
                      {run.refined_topic || run.topic}
                    </span>
                    <span
                      className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] ${
                        STATUS_CLASS[run.status] ?? "bg-accent text-muted"
                      }`}
                    >
                      {t(STATUS_KEYS[run.status] ?? "research.statusAbandoned")}
                    </span>
                  </div>
                  {run.refined_topic && run.topic && run.refined_topic !== run.topic && (
                    <p className="mt-1 truncate text-xs text-muted">
                      {t("research.refinedFrom", { topic: run.topic })}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                    <span>{t(`chat.researchDepth_${run.depth}`)}</span>
                    <span>{t(`chat.researchMode_${run.mode}`)}</span>
                    <span>{t("research.subtopicCount", { n: String(run.subtopics.length) })}</span>
                    {run.failed_subtopics.length > 0 && (
                      <span className="text-danger">
                        {t("research.failedCount", { n: String(run.failed_subtopics.length) })}
                      </span>
                    )}
                    <span className="ml-auto">{dateLabel(run.created_at)}</span>
                  </div>
                  {run.session_title && (
                    <p className="mt-1.5 truncate text-xs text-muted">
                      {t("research.session")}：{run.session_title}
                    </p>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
