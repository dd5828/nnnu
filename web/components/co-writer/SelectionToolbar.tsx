"use client";

/** 选区改写工具条（§7.13）：五个预设动作 + 自由指令 + 知识库多选 + 联网开关。
 *
 * 只管收集参数并回调 onRun，编排（先 flush 自动保存再发请求）在工作台页——
 * 工具条一旦知道「保存」这回事，两个状态机就会开始互相打架。
 * 选区下标是**码点**（页面用 codePointIndex 换好再传进来）。
 */

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useKbList } from "@/hooks/useKnowledge";
import { CO_WRITER_ACTIONS, MAX_SELECTION_CHARS, actionLabelKey } from "@/lib/co-writer";
import type { CoWriterAction } from "@/types/api";

export interface CoWriterSelection {
  /** 码点下标，直接交给后端切片。 */
  start: number;
  end: number;
  /** 选区长度（码点）。 */
  length: number;
  /** 给用户看的一小段选区开头。 */
  preview: string;
}

export interface CoWriterRunInput {
  action: CoWriterAction;
  instruction: string;
  kbIds: string[];
  useWeb: boolean;
}

const PRESET_ACTIONS = CO_WRITER_ACTIONS.filter((action) => action !== "free");

export default function SelectionToolbar({
  selection,
  busy,
  onRun,
}: {
  selection: CoWriterSelection | null;
  busy: boolean;
  onRun: (input: CoWriterRunInput) => void;
}) {
  const { t } = useI18n();
  const { data } = useKbList();
  const [instruction, setInstruction] = useState("");
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [useWeb, setUseWeb] = useState(false);
  const [kbOpen, setKbOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!kbOpen) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setKbOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [kbOpen]);

  if (selection === null) {
    return (
      <div className="border-b border-border p-3">
        <p className="text-xs text-muted">{t("coWriter.selectionHint")}</p>
      </div>
    );
  }

  const readyKbs = (data?.kbs ?? []).filter((kb) => kb.status === "ready" && kb.active_version > 0);
  const tooLong = selection.length > MAX_SELECTION_CHARS;
  const frozen = busy || tooLong;

  const run = (action: CoWriterAction) => {
    if (frozen || (action === "free" && !instruction.trim())) {
      return;
    }
    onRun({ action, instruction: instruction.trim(), kbIds, useWeb });
  };

  const toggleKb = (id: string) => {
    setKbIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  return (
    <div ref={rootRef} data-testid="cw-toolbar" className="space-y-2 border-b border-border p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="shrink-0 text-xs font-medium">{t("coWriter.startEdit")}</span>
        <span className="min-w-0 truncate text-[11px] text-muted">
          {t("coWriter.selectionChars", { n: String(selection.length) })} · {selection.preview}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {PRESET_ACTIONS.map((action) => (
          <button
            key={action}
            type="button"
            data-testid={`cw-action-${action}`}
            disabled={frozen}
            onClick={() => run(action)}
            className="rounded-full border border-border px-2.5 py-1 text-xs transition-colors hover:bg-accent disabled:opacity-50"
          >
            {t(actionLabelKey(action))}
          </button>
        ))}
      </div>

      <div className="flex gap-1.5">
        <input
          type="text"
          data-testid="cw-instruction"
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          placeholder={t("coWriter.instructionPlaceholder")}
          disabled={frozen}
          className="min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs outline-none placeholder:text-muted focus:border-primary disabled:opacity-50"
        />
        <button
          type="button"
          data-testid="cw-action-free"
          disabled={frozen || !instruction.trim()}
          onClick={() => run("free")}
          className="shrink-0 rounded-lg border border-border px-2.5 py-1.5 text-xs transition-colors hover:bg-accent disabled:opacity-50"
        >
          {t("coWriter.action_free")}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <div className="relative">
          <button
            type="button"
            data-testid="cw-kb-toggle"
            aria-expanded={kbOpen}
            disabled={frozen}
            onClick={() => setKbOpen((prev) => !prev)}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 transition-colors hover:bg-accent disabled:opacity-50"
          >
            {t("coWriter.useKb")}
            {kbIds.length > 0 && <span className="text-muted">· {kbIds.length}</span>}
            <ChevronDown className="h-3 w-3" />
          </button>
          {kbOpen && (
            <div className="absolute left-0 top-full z-40 mt-1 w-[min(280px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-lg">
              {readyKbs.length === 0 ? (
                <div className="px-3 py-2 text-[11px] text-muted">{t("coWriter.kbNone")}</div>
              ) : (
                <div className="max-h-[240px] overflow-y-auto">
                  {readyKbs.map((kb) => (
                    <button
                      key={kb.id}
                      type="button"
                      data-testid="cw-kb-option"
                      data-id={kb.id}
                      onClick={() => toggleKb(kb.id)}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent"
                    >
                      <input
                        type="checkbox"
                        readOnly
                        checked={kbIds.includes(kb.id)}
                        tabIndex={-1}
                        className="pointer-events-none accent-primary"
                      />
                      <span className="min-w-0 flex-1 truncate">{kb.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <label className="inline-flex cursor-pointer items-center gap-1.5">
          <input
            type="checkbox"
            data-testid="cw-web"
            checked={useWeb}
            disabled={frozen}
            onChange={(event) => setUseWeb(event.target.checked)}
            className="accent-primary"
          />
          {t("coWriter.useWeb")}
        </label>

        {busy && (
          <span className="ml-auto inline-flex items-center gap-1.5 text-muted">
            <Loader2 className="h-3 w-3 animate-spin" />
            {t("coWriter.editing")}
          </span>
        )}
      </div>

      {tooLong && (
        <p className="text-[11px] text-danger">
          {t("coWriter.selectionTooLong", { max: String(MAX_SELECTION_CHARS) })}
        </p>
      )}
    </div>
  );
}
