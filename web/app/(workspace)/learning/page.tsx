"use client";

/** 学习看板列表（§7.5）：路径卡片 + 空态提示。
 *
 * 路径**不在这个页面新建**（拍板 #3）：它由聊天里的 `mastery` 工具生成，
 * 所以空态给的是一句「怎么说」，不是「新建」按钮。
 */

import Link from "next/link";
import { GraduationCap, Loader2 } from "lucide-react";
import { useLearningPaths } from "@/hooks/useLearning";
import { useI18n } from "@/hooks/useI18n";
import { useNow } from "@/hooks/useNow";
import { nextActionKey, pathHref, reviewWhen } from "@/lib/learning";
import type { LearningPathCard } from "@/types/api";

function PathCard({ card, now }: { card: LearningPathCard; now: number }) {
  const { t } = useI18n();
  // 进度 = 已过门 / 总数（不再随时间往回掉：到期待复习仍算已掌握）
  const percent = Math.round(card.stats.progress * 100);
  const when = reviewWhen(card.next_review_at, now);
  return (
    <li data-testid="l-card" data-id={card.id}>
      <Link
        href={pathHref(card.id)}
        className="flex flex-col gap-2 rounded-xl border border-border bg-surface p-3 transition-colors hover:border-primary/40"
      >
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-medium">{card.title}</h2>
            <p className="mt-0.5 truncate text-[11px] text-muted">
              {card.topic}
              {card.summary ? ` · ${card.summary}` : ""}
            </p>
          </div>
          {card.next_title && (
            <span
              data-testid="l-card-next"
              data-action={card.next_action ?? "complete"}
              className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-primary"
            >
              {t("learning.currentShort", { title: card.next_title })}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 text-[11px] text-muted">
          <span className="inline-block h-1.5 flex-1 overflow-hidden rounded-full bg-accent">
            <span
              data-testid="l-card-progress"
              data-value={percent}
              className="block h-full rounded-full bg-primary"
              style={{ width: `${percent}%` }}
            />
          </span>
          <span>{percent}%</span>
        </div>

        <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted">
          <span>{t("learning.cardMastered", { n: String(card.stats.mastered) })}</span>
          <span>{t("learning.cardTotal", { n: String(card.stats.total) })}</span>
          <span className={card.stats.weak > 0 ? "text-danger" : undefined}>
            {t("learning.statWeak", { n: String(card.stats.weak) })}
          </span>
          {/* 到期数 0 不渲染：列表里一排「到期 0」是噪声 */}
          {card.stats.due > 0 && (
            <span data-testid="l-card-due" data-value={card.stats.due} className="text-danger">
              {t("learning.statDue", { n: String(card.stats.due) })}
            </span>
          )}
          {card.next_action && card.next_action !== "complete" && (
            <span data-testid="l-card-action">{t(nextActionKey(card.next_action))}</span>
          )}
          <span className="ml-auto" data-testid="l-card-review">
            {t(when.key, when.vars)}
          </span>
        </div>
      </Link>
    </li>
  );
}

export default function LearningPage() {
  const { t } = useI18n();
  const now = useNow();
  const { data, isLoading, error } = useLearningPaths();
  const paths = data?.paths ?? [];

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div>
          <h1 className="text-xl font-semibold">{t("learning.title")}</h1>
          <p className="mt-0.5 text-xs text-muted">{t("learning.desc")}</p>
        </div>

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{String(error)}</p>
        ) : paths.length === 0 ? (
          <div
            data-testid="l-empty"
            className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center"
          >
            <GraduationCap className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("learning.empty")}</p>
            <p className="max-w-sm text-xs text-muted">{t("learning.emptyHint")}</p>
          </div>
        ) : (
          <ul className="space-y-2" data-testid="l-list">
            {paths.map((card) => (
              <PathCard key={card.id} card={card} now={now} />
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
