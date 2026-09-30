"use client";

/**
 * 一次调研的详情：大纲 + 报告 + 来源，以及「在会话里打开」。
 *
 * 报告正文不存库（助理消息 id 在能力返回之后才生成），详情端点按
 * `answer_message_id` 往后现查第一条助手消息——所以这里拿到的与聊天页看到的是同一条。
 * 来源用聊天页同一个 `CitationsPanel`（网页来源点开是新窗口，验真伪用）。
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Loader2, MessageSquare } from "lucide-react";
import Markdown from "@/components/chat/Markdown";
import CitationsPanel from "@/components/chat/CitationsPanel";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { useResearchRun } from "@/hooks/useResearch";
import type { ResearchRunStatus } from "@/types/api";

const CAPABILITY_RESEARCH = "deep_research";

const STATUS_KEYS: Record<ResearchRunStatus, string> = {
  confirming: "research.statusConfirming",
  researching: "research.statusResearching",
  reported: "research.statusReported",
  partial: "research.statusPartial",
  abandoned: "research.statusAbandoned",
};

/** 没成稿时给一句「为什么没有」——按状态说实话，别一律「暂无报告」。
 *
 * reported/partial 却没有报告只有一种可能：会话（或那条消息）被删了，消息跟着没
 * ——调研行是软引用，会留着（见 v10 建表注释），所以命中这一支时要说「不在了」。 */
const NO_REPORT_KEYS: Record<ResearchRunStatus, string> = {
  confirming: "research.noReportConfirming",
  researching: "research.noReportResearching",
  reported: "research.noReportDeleted",
  partial: "research.noReportDeleted",
  abandoned: "research.noReportAbandoned",
};

function dateLabel(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

export default function ResearchRunPage() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const runId = params?.id ?? null;
  const { data, isLoading, error } = useResearchRun(runId);
  const attachSession = useChatStore((s) => s.attachSession);
  const setCapability = useChatStore((s) => s.setCapability);
  const router = useRouter();

  const openSession = () => {
    if (!data) {
      return;
    }
    attachSession(data.session_id);
    setCapability(CAPABILITY_RESEARCH); // 不切能力，进去打的第一句会按旧能力跑
    router.push("/");
  };

  const failed = new Set(data?.failed_subtopics ?? []);

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <Link
          href="/research"
          className="inline-flex w-fit items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {t("research.backToList")}
        </Link>

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error || !data ? (
          <p className="text-sm text-danger">{error ? String(error) : t("research.notFound")}</p>
        ) : (
          <>
            <div data-testid="research-detail">
              <div className="flex items-start gap-2">
                <h1 className="min-w-0 flex-1 break-words text-xl font-semibold">
                  {data.refined_topic || data.topic}
                </h1>
                <span className="shrink-0 rounded-full bg-accent px-2 py-0.5 text-[11px] text-muted">
                  {t(STATUS_KEYS[data.status] ?? "research.statusAbandoned")}
                </span>
              </div>
              {data.refined_topic && data.topic && data.refined_topic !== data.topic && (
                <p className="mt-1 text-xs text-muted">
                  {t("research.refinedFrom", { topic: data.topic })}
                </p>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                <span>{t(`chat.researchDepth_${data.depth}`)}</span>
                <span>{t(`chat.researchMode_${data.mode}`)}</span>
                <span>{dateLabel(data.created_at)}</span>
              </div>
              {data.session_title && (
                <button
                  type="button"
                  data-testid="research-open-session"
                  onClick={openSession}
                  className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
                >
                  <MessageSquare className="h-3.5 w-3.5" />
                  {t("research.openSession")}：{data.session_title}
                </button>
              )}
            </div>

            <section
              data-testid="research-outline"
              className="rounded-xl border border-border bg-surface p-4"
            >
              <h2 className="mb-2 text-sm font-medium">{t("research.outline")}</h2>
              {data.subtopics.length === 0 ? (
                <p className="text-xs text-muted">{t("research.noOutline")}</p>
              ) : (
                <ol className="space-y-2">
                  {data.subtopics.map((item, index) => (
                    <li key={`${item.title}-${index}`} className="text-sm">
                      <span className="mr-1.5 inline-block min-w-5 rounded bg-accent px-1 text-center text-xs font-medium text-primary">
                        {index + 1}
                      </span>
                      <span className="font-medium">{item.title}</span>
                      {failed.has(item.title) && (
                        <span className="ml-1.5 text-xs text-danger">{t("research.failed")}</span>
                      )}
                      {item.overview && (
                        <p className="mt-0.5 text-xs text-muted">{item.overview}</p>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section className="rounded-xl border border-border bg-surface p-4">
              <h2 className="mb-2 text-sm font-medium">{t("research.report")}</h2>
              {data.report ? (
                <div data-testid="research-report">
                  <Markdown text={data.report.content_md} />
                  <CitationsPanel sources={data.report.citations} />
                </div>
              ) : (
                <p data-testid="research-no-report" className="text-xs text-muted">
                  {t(NO_REPORT_KEYS[data.status] ?? "research.noReportAbandoned")}
                </p>
              )}
            </section>
          </>
        )}
      </div>
    </main>
  );
}
