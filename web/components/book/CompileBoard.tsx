"use client";

/** 编译看板（compiling / paused）：逐章逐块的状态格 + 总进度 + 暂停/继续。
 *
 * 数据就吃 GET 详情里页 meta 的 blocks 摘要（id/type/status 三件），状态由上层
 * 轮询刷新；这里不自己发请求。块格子的 title 里带类型与状态，鼠标扫一眼就知道
 * 哪块卡住了。
 */

import { Loader2, Pause, Play } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import {
  blockStatusClass,
  blockStatusKey,
  blockTypeKey,
  chapterRows,
  compileStats,
} from "@/lib/book";
import { ProgressBar } from "@/components/knowledge/status";
import type { BookPageMeta, BookSpine, BookStatus } from "@/types/api";

interface Props {
  spine: BookSpine;
  pages: BookPageMeta[];
  status: BookStatus;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
}

export default function CompileBoard({ spine, pages, status, busy, onPause, onResume }: Props) {
  const { t } = useI18n();
  const stats = compileStats(pages);
  const rows = chapterRows(spine, pages);

  return (
    <div data-testid="bk-compile" className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center gap-3">
        <span className="inline-flex items-center gap-1.5 text-sm">
          {status === "compiling" && <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />}
          {status === "compiling" ? t("book.status_compiling") : t("book.status_paused")}
        </span>
        <span data-testid="bk-compile-count" className="text-xs text-muted">
          {t("book.progressBlocks", { done: String(stats.done), total: String(stats.total) })}
        </span>
        {stats.error > 0 && (
          <span className="text-xs text-danger">
            {t("book.progressErrors", { n: String(stats.error) })}
          </span>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {status === "compiling" ? (
            <button
              type="button"
              data-testid="bk-pause"
              disabled={busy}
              onClick={onPause}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
            >
              <Pause className="h-3.5 w-3.5" />
              {t("book.pause")}
            </button>
          ) : (
            <button
              type="button"
              data-testid="bk-resume"
              disabled={busy}
              onClick={onResume}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
            >
              {busy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {t("book.resume")}
            </button>
          )}
        </div>
      </div>

      <div className="mt-3">
        <ProgressBar value={stats.percent / 100} />
      </div>

      <ul className="mt-3 space-y-2">
        {rows.map(({ chapter, page }) => {
          const done = page?.blocks_done ?? 0;
          const total = page?.block_count ?? chapter.blocks_plan.length;
          return (
            <li
              key={chapter.key}
              data-testid="bk-compile-chapter"
              data-key={chapter.key}
              className="flex items-center gap-3"
            >
              <span className="min-w-0 flex-1 truncate text-xs">{chapter.title}</span>
              <span className="flex shrink-0 flex-wrap items-center gap-1">
                {(page?.blocks ?? []).map((block) => (
                  <span
                    key={block.id}
                    data-testid="bk-block-cell"
                    data-status={block.status}
                    title={`${t(blockTypeKey(block.type))} · ${t(blockStatusKey(block.status))}`}
                    className={`h-2.5 w-5 rounded-sm ${blockStatusClass(block.status)}`}
                  />
                ))}
              </span>
              <span className="w-12 shrink-0 text-right text-xs text-muted">
                {done}/{total}
              </span>
            </li>
          );
        })}
      </ul>

      <p className="mt-3 text-xs text-muted">{t("book.compileHint")}</p>
    </div>
  );
}
