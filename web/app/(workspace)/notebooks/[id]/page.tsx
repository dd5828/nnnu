"use client";

/**
 * 笔记本详情（§7.15 极简版）：记录列表（Markdown 渲染）+ 手写笔记 + 删除。
 *
 * 记录只增不改（后端本批没给 PATCH）：写错了删掉重存，别让「编辑」把批一的
 * 复杂度拉满——正式版的编辑/移动随 P9。
 */

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Loader2, Plus, Trash2 } from "lucide-react";
import Markdown from "@/components/chat/Markdown";
import { useAddRecord, useDeleteRecord, useNotebook } from "@/hooks/useNotebooks";
import { useI18n } from "@/hooks/useI18n";
import type { NotebookRecordType } from "@/types/api";

const TYPE_LABEL_KEYS: Record<NotebookRecordType, string> = {
  note: "notebooks.typeNote",
  chat: "notebooks.typeChat",
  solve: "notebooks.typeSolve",
};

function dateLabel(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

export default function NotebookDetailPage() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const notebookId = params?.id ?? null;
  const { data, isLoading, error } = useNotebook(notebookId);
  const add = useAddRecord();
  const remove = useDeleteRecord();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const records = data?.records ?? [];

  const submit = async () => {
    if (!body.trim() || !notebookId) {
      setFormError(t("notebooks.bodyRequired"));
      return;
    }
    setFormError(null);
    try {
      await add.mutateAsync({
        notebookId,
        type: "note",
        title: title.trim(),
        content_md: body,
      });
      setTitle("");
      setBody("");
    } catch (err) {
      setFormError(String(err));
    }
  };

  const handleDelete = (recordId: string) => {
    if (!notebookId || !window.confirm(t("notebooks.deleteRecordConfirm"))) {
      return;
    }
    remove.mutate({ notebookId, recordId });
  };

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <Link
          href="/notebooks"
          className="inline-flex w-fit items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {t("notebooks.backToList")}
        </Link>

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error || !data ? (
          <p className="text-sm text-danger">{error ? String(error) : t("notebooks.notFound")}</p>
        ) : (
          <>
            <div>
              <h1 className="text-xl font-semibold">{data.name}</h1>
              {data.description && <p className="mt-0.5 text-xs text-muted">{data.description}</p>}
              <p className="mt-1 text-xs text-muted">
                {t("notebooks.recordCount", { n: String(records.length) })}
              </p>
            </div>

            <div className="space-y-2 rounded-xl border border-border bg-surface p-4">
              <input
                data-testid="nb-note-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder={t("notebooks.noteTitlePlaceholder")}
                className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
              />
              <textarea
                data-testid="nb-note-body"
                value={body}
                onChange={(event) => setBody(event.target.value)}
                placeholder={t("notebooks.notePlaceholder")}
                rows={3}
                className="w-full resize-y rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
              />
              {formError && <p className="text-xs text-danger">{formError}</p>}
              <button
                type="button"
                data-testid="nb-note-add"
                onClick={() => void submit()}
                disabled={add.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
              >
                {add.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Plus className="h-3.5 w-3.5" />
                )}
                {t("notebooks.addNote")}
              </button>
            </div>

            {records.length === 0 ? (
              <p className="rounded-xl border border-dashed border-border py-10 text-center text-sm text-muted">
                {t("notebooks.noRecords")}
              </p>
            ) : (
              <ul className="space-y-3">
                {records.map((record) => (
                  <li
                    key={record.id}
                    data-testid="nb-record"
                    data-id={record.id}
                    className="rounded-xl border border-border bg-surface p-4"
                  >
                    <div className="flex items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="truncate text-sm font-medium">{record.title}</span>
                          <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
                            {t(TYPE_LABEL_KEYS[record.type] ?? "notebooks.typeNote")}
                          </span>
                          <span className="shrink-0 text-[11px] text-muted">
                            {dateLabel(record.created_at)}
                          </span>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleDelete(record.id)}
                        title={t("notebooks.deleteRecord")}
                        className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                    <div className="mt-2 max-w-full">
                      <Markdown text={record.content_md} />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </main>
  );
}
