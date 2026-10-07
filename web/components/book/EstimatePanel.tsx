"use client";

/** 编译估算面板（draft）：按当前目录现算的数字**全部来自后端**（GET 现算），
 *  这里只做格式化——页数/块数/token/费用/时间的公式不在前端镜像。 */

import { Clock, Coins, Layers } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { formatCost, formatTokens, splitDuration } from "@/lib/book";
import type { BookEstimate } from "@/types/api";

export default function EstimatePanel({ estimate }: { estimate: BookEstimate }) {
  const { t } = useI18n();
  const totals = estimate.totals;
  const time = splitDuration(totals.seconds);

  return (
    <div data-testid="bk-estimate" className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-medium">{t("book.estimateTitle")}</h2>
        <span className="text-xs text-muted">{t("book.estimateHint")}</span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <div className="rounded-lg bg-accent/60 px-3 py-2">
          <div className="flex items-center gap-1.5 text-muted">
            <Layers className="h-3.5 w-3.5" />
            {t("book.estimateChapters", { n: String(totals.chapters) })}
          </div>
          <div className="mt-1 text-sm">
            {t("book.estimateBlocks", { n: String(totals.blocks) })}
          </div>
        </div>
        <div className="rounded-lg bg-accent/60 px-3 py-2">
          <div className="text-muted">Token</div>
          <div data-testid="bk-estimate-tokens" className="mt-1 text-sm">
            {formatTokens(totals.tokens)}
          </div>
        </div>
        <div className="rounded-lg bg-accent/60 px-3 py-2">
          <div className="flex items-center gap-1.5 text-muted">
            <Coins className="h-3.5 w-3.5" />
            {t("book.estimateCostLabel")}
          </div>
          <div data-testid="bk-estimate-cost" className="mt-1 text-sm">
            {estimate.priced ? formatCost(totals.cost) : t("book.estimateNoPrice")}
          </div>
        </div>
        <div className="rounded-lg bg-accent/60 px-3 py-2">
          <div className="flex items-center gap-1.5 text-muted">
            <Clock className="h-3.5 w-3.5" />
            {t("book.estimateTimeLabel")}
          </div>
          <div className="mt-1 text-sm">
            {time.minutes > 0
              ? t("book.estimateTimeMinutes", { m: String(time.minutes), s: String(time.seconds) })
              : t("book.estimateTimeSeconds", { s: String(time.seconds) })}
          </div>
        </div>
      </div>

      <ul className="mt-3 space-y-1">
        {estimate.chapters.map((chapter) => {
          const perChapter = splitDuration(chapter.seconds);
          return (
            <li
              key={chapter.key}
              data-testid="bk-estimate-chapter"
              className="flex items-center gap-2 text-xs text-muted"
            >
              <span className="min-w-0 flex-1 truncate">{chapter.title}</span>
              <span className="shrink-0">
                {t("book.chapterBlocks", { n: String(chapter.blocks) })}
              </span>
              <span className="w-14 shrink-0 text-right">{formatTokens(chapter.tokens)}</span>
              <span className="w-20 shrink-0 text-right">
                {perChapter.minutes > 0
                  ? t("book.estimateTimeMinutes", {
                      m: String(perChapter.minutes),
                      s: String(perChapter.seconds),
                    })
                  : t("book.estimateTimeSeconds", { s: String(perChapter.seconds) })}
              </span>
            </li>
          );
        })}
      </ul>
      {estimate.model && (
        <p className="mt-2 text-xs text-muted">
          {t("book.estimateModel", { model: estimate.model })}
        </p>
      )}
    </div>
  );
}
