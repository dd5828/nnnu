"use client";

/**
 * 引用面板（§6.1 citation 事件 / done.citations）：来源列表。
 *
 * 落点分三支（见 `lib/citations.ts`）：联网来源是新窗口可点的外链
 * （§7.6 验收要求来源「可点击验证」）、知识库引用跳知识中心详情页并直接开阅读器
 * 到那一页（深链接 `?doc=&page=`）、附件引用没有可跳的页面，保持纯文本。
 */

import { useState } from "react";
import Link from "next/link";
import { BookOpen, ChevronDown, ExternalLink } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { citationTarget } from "@/lib/citations";
import type { CitationSource } from "@/types/stream";

/** 先露几条：检索默认 top_k=5，一次列全把回答挤没了；剩下的折起来，想看得自己展 */
const VISIBLE_SOURCES = 3;

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
          const target = citationTarget(source);
          const page =
            source.page !== null && source.page !== undefined
              ? ` · ${t("chat.page", { n: String(source.page) })}`
              : "";
          const label = `${target.label}${page}`;
          return (
            <li key={`${source.doc_id}-${index}`} className="text-xs">
              <span className="mr-1 inline-block min-w-4 rounded bg-accent px-1 text-center font-medium text-primary">
                {index + 1}
              </span>
              {target.kind === "external" ? (
                <a
                  href={target.href}
                  target="_blank"
                  rel="noreferrer"
                  data-testid="citation-external"
                  title={source.doc_id}
                  className="inline-flex items-center gap-1 text-primary hover:underline"
                >
                  {label}
                  <ExternalLink className="h-3 w-3 shrink-0" />
                </a>
              ) : target.kind === "internal" ? (
                <Link
                  href={target.href}
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
