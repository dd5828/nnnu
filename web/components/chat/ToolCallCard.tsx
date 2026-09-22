"use client";

/** 工具调用卡片（§6.1 tool_call/tool_result）：状态 + 参数/结果折叠（detail 默认折叠）。 */

import { useState } from "react";
import { Check, ChevronDown, Loader2, Wrench, X } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { UiToolCall } from "@/hooks/useChat";

export default function ToolCallCard({ call }: { call: UiToolCall }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const running = call.ok === null;
  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-border/70 bg-surface text-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 transition-colors hover:bg-accent/40"
      >
        {running ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
        ) : call.ok ? (
          <Check className="h-3.5 w-3.5 text-success" />
        ) : (
          <X className="h-3.5 w-3.5 text-danger" />
        )}
        <Wrench className="h-3.5 w-3.5 text-muted" />
        <span className="font-mono text-xs font-medium">{call.name}</span>
        {!running && (
          <span
            className={`text-xs ${call.ok ? "text-success" : "text-danger"}`}
          >
            {call.ok ? t("chat.toolOk") : t("chat.toolFailed")}
          </span>
        )}
        {!running && call.summary && (
          <span className="min-w-0 flex-1 truncate text-left text-muted">{call.summary}</span>
        )}
        <ChevronDown
          className={`ml-auto h-3.5 w-3.5 text-muted transition-transform ${open ? "" : "-rotate-90"}`}
        />
      </button>
      {open && (
        <div className="space-y-1 border-t border-border/70 px-3 py-2">
          <div className="text-xs text-muted">
            <span className="font-medium">{t("chat.toolArgs")}</span>
            <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono">
              {JSON.stringify(call.args, null, 2)}
            </pre>
          </div>
          {call.summary && (
            <div className="text-xs text-muted">
              <span className="font-medium">{t("chat.toolResult")}</span>
              <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono">
                {call.summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
