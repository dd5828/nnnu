"use client";

/** 时间线块：竖线 + 节点圆点，when 列在左、标题与细节在右（窄屏两行叠）。 */

import { useI18n } from "@/hooks/useI18n";
import { parseTimeline } from "@/lib/book";
import type { BookBlock } from "@/types/api";

export default function TimelineBlock({ block }: { block: BookBlock }) {
  const { t } = useI18n();
  const view = parseTimeline(block.payload);

  if (view.events.length === 0) {
    return <p className="text-xs text-muted">{t("book.readerEmpty")}</p>;
  }

  return (
    <ol className="space-y-3" data-testid="bk-timeline" data-block-id={block.id}>
      {view.events.map((event, index) => (
        <li key={index} className="flex gap-3">
          <div className="flex w-20 shrink-0 justify-end pt-0.5 text-xs text-muted">
            {event.when}
          </div>
          <div className="relative flex flex-col items-center pt-1.5">
            <span aria-hidden className="size-2 rounded-full bg-primary" />
            {index < view.events.length - 1 && (
              <span aria-hidden className="mt-1 w-px flex-1 bg-border" />
            )}
          </div>
          <div className="min-w-0 flex-1 pb-1">
            <p className="text-sm font-medium">{event.title}</p>
            {event.detail && (
              <p className="mt-0.5 text-xs leading-relaxed text-muted">{event.detail}</p>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
