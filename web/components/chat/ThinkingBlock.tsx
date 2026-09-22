"use client";

/** 思考块（§6.1 thinking_delta/done）：可折叠面板，流式时默认展开。 */

import { useState } from "react";
import { Brain, ChevronDown } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";

export default function ThinkingBlock({ text, streaming }: { text: string; streaming: boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(streaming);
  if (!text) {
    return null;
  }
  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-border/70 bg-accent/40 text-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-muted transition-colors hover:bg-accent/60"
      >
        <Brain className="h-3.5 w-3.5" />
        <span className="font-medium">{t("chat.thinking")}</span>
        {streaming && <span className="animate-pulse text-xs">…</span>}
        <ChevronDown
          className={`ml-auto h-3.5 w-3.5 transition-transform ${open ? "" : "-rotate-90"}`}
        />
      </button>
      {open && (
        <div className="max-h-64 overflow-y-auto whitespace-pre-wrap px-3 pb-2 text-muted">
          {text}
        </div>
      )}
    </div>
  );
}
