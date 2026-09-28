"use client";

/**
 * 引用面板（§6.1 citation 事件 / done.citations）：来源列表。
 *
 * 知识库引用可点：跳到知识中心详情页并直接开阅读器到那一页（深链接
 * `?doc=&page=`）。附件引用（kb === "attachment"）没有可跳的页面，保持纯文本。
 */

import { useState } from "react";
import Link from "next/link";
import { BookOpen, ChevronDown } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { CitationSource } from "@/types/stream";

/** 先露几条：检索默认 top_k=5，一次列全把回答挤没了；剩下的折起来，想看得自己展 */
const VISIBLE_SOURCES = 3;

/** 引用 → 知识中心深链接；附件引用返回 null（不跳）。 */
function locatePath(source: CitationSource): string | null {
  if (!source.kb || source.kb === "attachment") {
    return null;
  }
  const query = new URLSearchParams({ doc: source.doc_id });
  if (source.page !== null && source.page !== undefined) {
    query.set("page", String(source.page));
  }
  return `/knowledge/${source.kb}?${query.toString()}`;
}

export default function CitationsPanel({ sources }: { sources: CitationSource[] }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  if (sources.length === 0) {
    return null;
  }
  const hidden = sources.length - VISIBLE_SOURCES;
  const shown = expanded || hidden <= 0 ? sources : sources.slice(0, VISIBLE_SOURCES);
  return (
    <div className="mt-2 rounded-lg border border-border/70 bg-surface p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted">
        <BookOpen className="h-3.5 w-3.5" />
        {t("chat.citations")}（{sources.length}）
      </div>
      <ol className="space-y-1.5">
        {shown.map((source, index) => {
          const path = locatePath(source);
          const label = `${source.kb || source.doc_id}${
            source.page !== null && source.page !== undefined
              ? ` · ${t("chat.page", { n: String(source.page) })}`
              : ""
          }`;
          return (
            <li key={`${source.doc_id}-${index}`} className="text-xs">
              <span className="mr-1 inline-block min-w-4 rounded bg-accent px-1 text-center font-medium text-primary">
                {index + 1}
              </span>
              {path ? (
                <Link
                  href={path}
                  data-testid="citation-link"
                  className="text-muted hover:underline"
                >
                  {label}
                </Link>
              ) : (
                <span className="text-muted">{label}</span>
              )}
              <div className="mt-0.5 line-clamp-2 pl-5 text-muted">{source.snippet}</div>
            </li>
          );
        })}
      </ol>
      {hidden > 0 && (
        <button
          type="button"
          data-testid="citations-toggle"
          data-expanded={expanded ? "true" : "false"}
          onClick={() => setExpanded((value) => !value)}
          className="mt-1.5 inline-flex items-center gap-1 text-xs text-primary transition-colors hover:underline"
        >
          <ChevronDown
            className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-180" : ""}`}
          />
          {expanded ? t("chat.citationsLess") : t("chat.citationsMore", { n: String(hidden) })}
        </button>
      )}
    </div>
  );
}
