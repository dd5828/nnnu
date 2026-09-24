"use client";

/** 「存入笔记本」（§7.15 极简版）：把这条回答存进选中的笔记本。
 *
 * 标题不在这里造：后端按正文首个非空行推（`services/notebooks/service.py:_title_of`），
 * 前端再抄一份规则只会两边慢慢长歪。来源记会话 id，方便以后回溯到原始对话。
 */

import { useEffect, useRef, useState } from "react";
import { BookmarkPlus, Check, Loader2 } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { useAddRecord, useCreateNotebook, useNotebookList } from "@/hooks/useNotebooks";
import { recordTypeFor } from "@/lib/capabilities";

export default function SaveToNotebook({ content }: { content: string }) {
  const { t } = useI18n();
  const capability = useChatStore((s) => s.capability);
  const sessionId = useChatStore((s) => s.sessionId);
  const { data } = useNotebookList();
  const create = useCreateNotebook();
  const add = useAddRecord();
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const notebooks = data?.notebooks ?? [];
  const busy = add.isPending || create.isPending;

  useEffect(() => {
    if (!open) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const save = async (notebookId: string) => {
    setError(null);
    try {
      await add.mutateAsync({
        notebookId,
        type: recordTypeFor(capability),
        content_md: content,
        source_ref: sessionId,
      });
      setSaved(true);
      setOpen(false);
    } catch (err) {
      setError(String(err));
    }
  };

  /** 一本都没有时就地建一本再存（空手点按钮不该被弹回去先建本子）。 */
  const createAndSave = async () => {
    setError(null);
    try {
      const notebook = await create.mutateAsync({ name: t("notebooks.defaultName") });
      await save(notebook.id);
    } catch (err) {
      setError(String(err));
    }
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="save-to-notebook"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-muted transition-colors hover:bg-accent hover:text-foreground"
      >
        {saved ? <Check className="h-3 w-3" /> : <BookmarkPlus className="h-3 w-3" />}
        {saved ? t("notebooks.saved") : t("notebooks.save")}
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(280px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-lg">
          {notebooks.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-muted">{t("notebooks.noNotebooks")}</div>
          ) : (
            <div className="max-h-[240px] overflow-y-auto">
              {notebooks.map((notebook) => (
                <button
                  key={notebook.id}
                  type="button"
                  data-testid="notebook-option"
                  data-id={notebook.id}
                  disabled={busy}
                  onClick={() => void save(notebook.id)}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent disabled:opacity-50"
                >
                  <BookmarkPlus className="h-3.5 w-3.5 shrink-0 text-muted" />
                  <span className="min-w-0 flex-1 truncate">{notebook.name}</span>
                  <span className="shrink-0 text-[10px] text-muted">
                    {t("notebooks.recordCount", { n: String(notebook.record_count ?? 0) })}
                  </span>
                </button>
              ))}
            </div>
          )}
          <button
            type="button"
            data-testid="notebook-create-and-save"
            disabled={busy}
            onClick={() => void createAndSave()}
            className="flex w-full items-center gap-2 border-t border-border px-3 py-1.5 text-left text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
          >
            {busy ? (
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
            ) : (
              <BookmarkPlus className="h-3.5 w-3.5 shrink-0" />
            )}
            {t("notebooks.saveToNew")}
          </button>
          {error && <div className="px-3 py-1.5 text-[11px] text-danger">{error}</div>}
        </div>
      )}
    </div>
  );
}
