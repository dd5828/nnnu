"use client";

/** 改写过程折叠面板（§7.13）：工具调用轨迹 + 引用来源 + 接地降级说明。
 *
 * 数据全部来自发起改写那次的响应（trace/citations/degraded），不额外发请求：
 * 待确认编辑过期后这条响应就是唯一的过程记录。没有可展示的内容时整块不出现。
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { CoWriterTraceEntry } from "@/types/api";
import type { CitationSource } from "@/types/stream";

/** 工具参数里值得一看的键（rag 的 query/kb_name、web_search 的 query）。 */
function argsBrief(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of ["query", "kb_name", "url"]) {
    const value = args[key];
    if (typeof value === "string" && value) {
      parts.push(value);
    }
  }
  return parts.join(" · ");
}

export default function EditTracePanel({
  trace,
  citations,
  degraded,
}: {
  trace: CoWriterTraceEntry[];
  citations: CitationSource[];
  degraded: string | null;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);

  if (trace.length === 0 && citations.length === 0 && !degraded) {
    return null;
  }

  return (
    <div className="border-t border-border">
      <button
        type="button"
        data-testid="cw-trace-toggle"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-4 py-2 text-left text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
        )}
        {t("coWriter.trace")}
        {trace.length > 0 && (
          <span className="text-[10px]">
            {t("coWriter.traceCalls", { n: String(trace.length) })}
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-2 px-4 pb-3 text-xs">
          {degraded && <p className="text-muted">{t("coWriter.degradedKb")}</p>}
          {trace.map((entry) => (
            <div key={entry.call_id} className="flex items-start gap-2">
              <span
                className={`mt-0.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                  entry.ok ? "bg-success" : "bg-danger"
                }`}
              />
              <div className="min-w-0">
                <span className="font-medium">{entry.tool_name}</span>
                {argsBrief(entry.args) && (
                  <span className="ml-1.5 text-muted">{argsBrief(entry.args)}</span>
                )}
                {entry.summary && <p className="mt-0.5 line-clamp-2 text-muted">{entry.summary}</p>}
              </div>
            </div>
          ))}
          {citations.length > 0 && (
            <div className="space-y-1 border-t border-border pt-2">
              <div className="text-muted">{t("coWriter.citations")}</div>
              {citations.map((source, index) => (
                <div key={`${source.doc_id}-${index}`} className="text-muted">
                  <span className="font-medium text-foreground">{source.title || source.kb}</span>
                  {source.page !== null && <span> · p.{source.page}</span>}
                  <p className="line-clamp-2">{source.snippet}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
