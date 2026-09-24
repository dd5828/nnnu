"use client";

/**
 * 笔记本列表（§7.15 极简版）：一本一卡——记录条数、创建时间，可新建/删除。
 *
 * 正式版（移动/复制/导出、@引用、write_note 工具）在 P9，这里只做能用的最小集。
 */

import { useState } from "react";
import Link from "next/link";
import { Loader2, NotebookPen, Plus, Trash2 } from "lucide-react";
import { useCreateNotebook, useDeleteNotebook, useNotebookList } from "@/hooks/useNotebooks";
import { useI18n } from "@/hooks/useI18n";

function dateLabel(seconds: number): string {
  return new Date(seconds * 1000).toLocaleDateString();
}

export default function NotebooksPage() {
  const { t } = useI18n();
  const { data, isLoading, error } = useNotebookList();
  const create = useCreateNotebook();
  const remove = useDeleteNotebook();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const notebooks = data?.notebooks ?? [];

  const submit = async () => {
    if (!name.trim()) {
      setFormError(t("notebooks.nameRequired"));
      return;
    }
    setFormError(null);
    try {
      await create.mutateAsync({
        name: name.trim(),
        ...(description.trim() ? { description: description.trim() } : {}),
      });
      setName("");
      setDescription("");
      setFormOpen(false);
    } catch (err) {
      setFormError(String(err));
    }
  };

  const handleDelete = (id: string, title: string) => {
    if (window.confirm(t("notebooks.deleteConfirm", { name: title }))) {
      remove.mutate(id);
    }
  };

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div>
            <h1 className="text-xl font-semibold">{t("notebooks.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("notebooks.desc")}</p>
          </div>
          {!formOpen && (
            <button
              type="button"
              data-testid="nb-new"
              onClick={() => setFormOpen(true)}
              className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("notebooks.newNotebook")}
            </button>
          )}
        </div>

        {formOpen && (
          <div className="space-y-2 rounded-xl border border-border bg-surface p-4">
            <input
              data-testid="nb-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void submit();
                }
              }}
              placeholder={t("notebooks.namePlaceholder")}
              className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={t("notebooks.descPlaceholder")}
              className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            {formError && <p className="text-xs text-danger">{formError}</p>}
            <div className="flex gap-2">
              <button
                type="button"
                data-testid="nb-create"
                onClick={() => void submit()}
                disabled={create.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
              >
                {create.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {t("notebooks.create")}
              </button>
              <button
                type="button"
                onClick={() => {
                  setFormOpen(false);
                  setFormError(null);
                }}
                className="rounded-lg px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
              >
                {t("common.close")}
              </button>
            </div>
          </div>
        )}

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{String(error)}</p>
        ) : notebooks.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
            <NotebookPen className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("notebooks.empty")}</p>
            <p className="max-w-xs text-xs text-muted">{t("notebooks.emptyHint")}</p>
          </div>
        ) : (
          <ul className="space-y-2">
            {notebooks.map((notebook) => (
              <li
                key={notebook.id}
                data-testid="nb-card"
                data-id={notebook.id}
                className="rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40"
              >
                <div className="flex items-start gap-3">
                  <Link href={`/notebooks/${notebook.id}`} className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{notebook.name}</div>
                    {notebook.description && (
                      <p className="mt-0.5 truncate text-xs text-muted">{notebook.description}</p>
                    )}
                    <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted">
                      <span>
                        {t("notebooks.recordCount", { n: String(notebook.record_count ?? 0) })}
                      </span>
                      <span>{dateLabel(notebook.created_at)}</span>
                    </div>
                  </Link>
                  <button
                    type="button"
                    onClick={() => handleDelete(notebook.id, notebook.name)}
                    title={t("notebooks.deleteNotebook")}
                    className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
