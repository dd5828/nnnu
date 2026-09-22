"use client";

/** 引用面板（§6.1 citation 事件 / done.citations）：来源列表（P4 起支持点击定位页码）。 */

import { BookOpen } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { CitationSource } from "@/types/stream";

export default function CitationsPanel({ sources }: { sources: CitationSource[] }) {
  const { t } = useI18n();
  if (sources.length === 0) {
    return null;
  }
  return (
    <div className="mt-2 rounded-lg border border-border/70 bg-surface p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted">
        <BookOpen className="h-3.5 w-3.5" />
        {t("chat.citations")}（{sources.length}）
      </div>
      <ol className="space-y-1.5">
        {sources.map((source, index) => (
          <li key={`${source.doc_id}-${index}`} className="text-xs">
            <span className="mr-1 inline-block min-w-4 rounded bg-accent px-1 text-center font-medium text-primary">
              {index + 1}
            </span>
            <span className="text-muted">
              {source.kb || source.doc_id}
              {source.page !== null && source.page !== undefined
                ? ` · ${t("chat.page", { n: String(source.page) })}`
                : ""}
            </span>
            <div className="mt-0.5 line-clamp-2 pl-5 text-muted">{source.snippet}</div>
          </li>
        ))}
      </ol>
    </div>
  );
}
