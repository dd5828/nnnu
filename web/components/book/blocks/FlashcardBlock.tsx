"use client";

/** 闪卡块：点卡面翻正反，上一张/下一张走一轮；计数只表达「看到第几张」，
 *  进度落库的口径是页级 visited（闪卡不做逐卡持久化，§7.14 验收没这个要求）。 */

import { useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { parseFlashcards } from "@/lib/book";
import type { BookBlock } from "@/types/api";

export default function FlashcardBlock({ block }: { block: BookBlock }) {
  const { t } = useI18n();
  const view = parseFlashcards(block.payload);
  const [index, setIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);

  if (view.cards.length === 0) {
    return <p className="text-xs text-muted">{t("book.readerEmpty")}</p>;
  }
  const card = view.cards[Math.min(index, view.cards.length - 1)];
  const total = view.cards.length;

  const step = (delta: number) => {
    setIndex((prev) => (prev + delta + total) % total);
    setFlipped(false);
  };

  return (
    <div className="space-y-2" data-testid="bk-flashcard" data-block-id={block.id}>
      <button
        type="button"
        data-testid="bk-card"
        data-side={flipped ? "back" : "front"}
        onClick={() => setFlipped((prev) => !prev)}
        className="flex min-h-32 w-full flex-col items-center justify-center gap-2 rounded-xl border border-border bg-surface px-4 py-6 text-center transition-colors hover:border-primary/40"
      >
        <span className="text-[11px] uppercase tracking-wider text-muted">
          {flipped ? t("book.cardBack") : t("book.cardFront")}
        </span>
        <span className="text-sm leading-relaxed">{flipped ? card.back : card.front}</span>
        <span className="text-[11px] text-muted">{t("book.cardFlip")}</span>
      </button>
      <div className="flex items-center justify-between">
        <button
          type="button"
          data-testid="bk-card-prev"
          onClick={() => step(-1)}
          className="inline-flex items-center gap-0.5 rounded-lg border border-border px-2.5 py-1 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
          {t("book.cardPrev")}
        </button>
        <span className="text-xs text-muted">
          {t("book.cardCounter", { i: String(index + 1), n: String(total) })}
        </span>
        <button
          type="button"
          data-testid="bk-card-next"
          onClick={() => step(1)}
          className="inline-flex items-center gap-0.5 rounded-lg border border-border px-2.5 py-1 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          {t("book.cardNext")}
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}
