"use client";

/**
 * L2/L3 条目列表（§7.10 工作台）：查看 / 人工编辑（进保护集）/ 删除 / 交还自动管理。
 *
 * 未纳管（id 为空）的手写条目只读展示——下轮 consolidator 的 audit 会给它补 id，
 * 之后才可编辑（与后端 PATCH 的寻址口径一致）。删除走 window.confirm（列表页惯例）。
 */

import { useState } from "react";
import { Check, Loader2, Pencil, RotateCcw, Trash2, X } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useDeleteMemoryEntry, usePatchMemoryEntry } from "@/hooks/useMemory";
import { errorText } from "@/lib/errors";
import { originLabelKey, previewText } from "@/lib/memory";
import type { MemoryDocView, MemoryEntryView } from "@/types/api";

interface Props {
  layer: "l2" | "l3";
  doc: MemoryDocView;
}

export default function MemoryEntryList({ layer, doc }: Props) {
  const { t } = useI18n();
  const patch = usePatchMemoryEntry();
  const remove = useDeleteMemoryEntry();
  const [editing, setEditing] = useState<{ id: string; draft: string } | null>(null);
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const handleSave = async (entry: MemoryEntryView) => {
    if (!entry.id || !editing) {
      return;
    }
    setRowError(null);
    setBusyId(entry.id);
    try {
      await patch.mutateAsync({ layer, id: entry.id, text: editing.draft.trim() });
      setEditing(null);
    } catch (err) {
      setRowError({ id: entry.id, message: errorText(err, t("common.requestFailed")) });
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async (entry: MemoryEntryView) => {
    if (
      !entry.id ||
      !window.confirm(t("memory.deleteConfirm", { text: previewText(entry.text, 40) }))
    ) {
      return;
    }
    setRowError(null);
    setBusyId(entry.id);
    try {
      await remove.mutateAsync({ layer, id: entry.id });
    } catch (err) {
      setRowError({ id: entry.id, message: errorText(err, t("common.requestFailed")) });
    } finally {
      setBusyId(null);
    }
  };

  const handleRelease = async (entry: MemoryEntryView) => {
    if (!entry.id || !window.confirm(t("memory.releaseConfirm"))) {
      return;
    }
    setRowError(null);
    setBusyId(entry.id);
    try {
      await patch.mutateAsync({ layer, id: entry.id, managed: true });
    } catch (err) {
      setRowError({ id: entry.id, message: errorText(err, t("common.requestFailed")) });
    } finally {
      setBusyId(null);
    }
  };

  if (doc.entries.length === 0) {
    return (
      <p
        data-testid="memory-empty"
        className="rounded-xl border border-dashed border-border py-8 text-center text-sm text-muted"
      >
        {t("memory.emptyDoc")}
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {doc.entries.map((entry) => {
        const busy = entry.id !== null && busyId === entry.id;
        const editingThis = entry.id !== null && editing?.id === entry.id;
        return (
          <li
            key={entry.id ?? `anon-${entry.line}`}
            data-testid="memory-entry"
            data-id={entry.id ?? ""}
            data-layer={entry.layer}
            className="rounded-xl border border-border bg-surface p-3"
          >
            {editingThis && editing ? (
              <div className="space-y-2">
                <textarea
                  data-testid="memory-edit-input"
                  value={editing.draft}
                  onChange={(event) => setEditing({ id: editing.id, draft: event.target.value })}
                  rows={3}
                  className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary"
                />
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setEditing(null)}
                    className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent"
                  >
                    <X className="size-3.5" />
                    {t("common.cancel")}
                  </button>
                  <button
                    type="button"
                    data-testid="memory-edit-save"
                    disabled={busy || editing.draft.trim().length === 0}
                    onClick={() => void handleSave(entry)}
                    className="inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-xs text-primary-foreground disabled:opacity-50"
                  >
                    {busy ? (
                      <Loader2 className="size-3.5 animate-spin" />
                    ) : (
                      <Check className="size-3.5" />
                    )}
                    {t("common.save")}
                  </button>
                </div>
              </div>
            ) : (
              <>
                <p className="whitespace-pre-wrap break-words text-sm">{entry.text}</p>
                <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
                  <span>{entry.date}</span>
                  <span className="rounded bg-accent px-1.5 py-0.5">
                    {t(originLabelKey(entry.origin))}
                  </span>
                  {entry.edited && (
                    <span className="rounded bg-accent px-1.5 py-0.5 text-primary">
                      {t("memory.badgeEdited")}
                    </span>
                  )}
                  {entry.stale && (
                    <span
                      data-testid="memory-stale"
                      title={t("memory.staleHint")}
                      className="rounded border border-dashed border-danger px-1.5 py-0.5 text-danger"
                    >
                      {t("memory.badgeStale")}
                    </span>
                  )}
                  {entry.id === null && (
                    <span
                      title={t("memory.anonymousHint")}
                      className="rounded bg-accent px-1.5 py-0.5"
                    >
                      {t("memory.badgeAnonymous")}
                    </span>
                  )}
                  {entry.refs.length > 0 && (
                    <span title={entry.refs.join("\n")}>
                      {t("memory.refCount", { n: String(entry.refs.length) })}
                    </span>
                  )}
                  <span className="flex-1" />
                  {busy ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <span className="flex items-center gap-1">
                      <button
                        type="button"
                        data-testid="memory-edit"
                        disabled={entry.id === null}
                        title={entry.id === null ? t("memory.anonymousHint") : t("memory.edit")}
                        onClick={() => entry.id && setEditing({ id: entry.id, draft: entry.text })}
                        className="rounded p-1 hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <Pencil className="size-3.5" />
                      </button>
                      {entry.edited && (
                        <button
                          type="button"
                          data-testid="memory-release"
                          title={t("memory.releaseHint")}
                          onClick={() => void handleRelease(entry)}
                          className="rounded p-1 hover:bg-accent"
                        >
                          <RotateCcw className="size-3.5" />
                        </button>
                      )}
                      <button
                        type="button"
                        data-testid="memory-delete"
                        disabled={entry.id === null}
                        title={entry.id === null ? t("memory.anonymousHint") : t("memory.delete")}
                        onClick={() => void handleDelete(entry)}
                        className="rounded p-1 text-danger hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <Trash2 className="size-3.5" />
                      </button>
                    </span>
                  )}
                </div>
                {rowError?.id === entry.id && (
                  <p className="mt-1.5 text-xs text-danger">{rowError.message}</p>
                )}
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}
