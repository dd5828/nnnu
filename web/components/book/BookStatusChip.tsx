"use client";

/** 活书状态徽标（书库卡片 / 控制台顶栏共用；配色表在 lib/book.ts）。 */

import { useI18n } from "@/hooks/useI18n";
import { bookStatusClass, bookStatusKey } from "@/lib/book";
import type { BookStatus } from "@/types/api";

export default function BookStatusChip({ status }: { status: BookStatus }) {
  const { t } = useI18n();
  return (
    <span
      data-testid="bk-status"
      data-status={status}
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] ${bookStatusClass(status)}`}
    >
      {t(bookStatusKey(status))}
    </span>
  );
}
