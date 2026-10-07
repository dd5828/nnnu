"use client";

/** 成书目录（ready）：点章进阅读器；读完打勾、书签标记、没编出来的块给重编入口。 */

import Link from "next/link";
import { Bookmark, Check, RotateCcw } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { chapterRows } from "@/lib/book";
import type { BookPageMeta, BookSpine } from "@/types/api";

interface Props {
  bookId: string;
  spine: BookSpine;
  pages: BookPageMeta[];
  busy: boolean;
  onRecompileChapter: (chapterKey: string) => void;
}

export default function ChapterToc({ bookId, spine, pages, busy, onRecompileChapter }: Props) {
  const { t } = useI18n();
  const rows = chapterRows(spine, pages);

  return (
    <div data-testid="bk-toc" className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-medium">{t("book.tocTitle")}</h2>
        <span className="text-xs text-muted">{t("book.tocHint")}</span>
      </div>
      <ul className="mt-3 space-y-1.5">
        {rows.map(({ chapter, page }, index) => (
          <li
            key={chapter.key}
            data-testid="bk-toc-row"
            data-key={chapter.key}
            className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 transition-colors hover:border-primary/40"
          >
            <span className="w-5 shrink-0 text-xs text-muted">{index + 1}</span>
            {page ? (
              <Link href={`/book/${bookId}/${page.id}`} className="min-w-0 flex-1">
                <div className="truncate text-sm">{chapter.title}</div>
                {chapter.summary && (
                  <div className="mt-0.5 truncate text-xs text-muted">{chapter.summary}</div>
                )}
              </Link>
            ) : (
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">{chapter.title}</div>
              </div>
            )}
            {page && (
              <span className="shrink-0 text-xs text-muted">
                {t("book.chapterBlocks", { n: String(page.block_count) })}
              </span>
            )}
            {page?.visited && <Check className="h-3.5 w-3.5 shrink-0 text-success" />}
            {page?.bookmarked && <Bookmark className="h-3.5 w-3.5 shrink-0 text-primary" />}
            {page && page.blocks_error > 0 && (
              <button
                type="button"
                data-testid="bk-toc-recompile"
                disabled={busy}
                onClick={() => onRecompileChapter(chapter.key)}
                title={t("book.recompileChapter")}
                className="inline-flex shrink-0 items-center gap-1 rounded border border-danger/40 px-1.5 py-0.5 text-[11px] text-danger transition-colors hover:bg-accent disabled:opacity-40"
              >
                <RotateCcw className="h-3 w-3" />
                {t("book.progressErrors", { n: String(page.blocks_error) })}
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
