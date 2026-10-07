"use client";

/**
 * Co-Writer 文档列表（§7.13）：卡片 = 标题 + 正文预览 + 更新时间，可新建/删除。
 *
 * 新建后直接进工作台（建篇文档的目的就是写它）；建完空标题也合法，
 * 服务端会在第一次 PATCH 正文时按首行补标题。
 */

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2, PenLine, Plus, Trash2 } from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import { useCoWriterList, useCreateCoWriterDoc, useDeleteCoWriterDoc } from "@/hooks/useCoWriter";
import { useI18n } from "@/hooks/useI18n";
import { errorText } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { Language } from "@/i18n";

function timeLabel(seconds: number, lang: Language): string {
  return formatDateTime(seconds, lang);
}

export default function CoWriterPage() {
  const { t, lang } = useI18n();
  const router = useRouter();
  const { data, isLoading, error } = useCoWriterList();
  const create = useCreateCoWriterDoc();
  const remove = useDeleteCoWriterDoc();
  const [title, setTitle] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const docs = data?.docs ?? [];

  const submit = async () => {
    setFormError(null);
    try {
      const doc = await create.mutateAsync(title.trim() ? { title: title.trim() } : {});
      setTitle("");
      setFormOpen(false);
      router.push(`/co-writer/${doc.id}`);
    } catch (err) {
      setFormError(errorText(err, t("common.requestFailed")));
    }
  };

  const handleDelete = (id: string, docTitle: string) => {
    void confirmDialog({
      message: t("coWriter.deleteConfirm", {
        title: docTitle || t("coWriter.untitled"),
      }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        remove.mutate(id);
      }
    });
  };

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div>
            <h1 className="text-xl font-semibold">{t("coWriter.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("coWriter.desc")}</p>
          </div>
          {!formOpen && (
            <button
              type="button"
              data-testid="cw-new"
              onClick={() => setFormOpen(true)}
              className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("coWriter.newDoc")}
            </button>
          )}
        </div>

        {formOpen && (
          <div className="space-y-2 rounded-xl border border-border bg-surface p-4">
            <input
              data-testid="cw-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void submit();
                }
              }}
              placeholder={t("coWriter.titlePlaceholder")}
              className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            {formError && <p className="text-xs text-danger">{formError}</p>}
            <div className="flex gap-2">
              <button
                type="button"
                data-testid="cw-create"
                onClick={() => void submit()}
                disabled={create.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
              >
                {create.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {t("coWriter.create")}
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
          <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
        ) : docs.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
            <PenLine className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("coWriter.empty")}</p>
            <p className="max-w-xs text-xs text-muted">{t("coWriter.emptyHint")}</p>
          </div>
        ) : (
          <ul className="space-y-2">
            {docs.map((doc) => (
              <li
                key={doc.id}
                data-testid="cw-card"
                data-id={doc.id}
                className="rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40"
              >
                <div className="flex items-start gap-3">
                  <Link href={`/co-writer/${doc.id}`} className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">
                      {doc.title || t("coWriter.untitled")}
                    </div>
                    {doc.preview && (
                      <p className="mt-0.5 truncate text-xs text-muted">{doc.preview}</p>
                    )}
                    <div className="mt-1 text-xs text-muted">
                      {t("coWriter.updatedAt", { time: timeLabel(doc.updated_at, lang) })}
                    </div>
                  </Link>
                  <button
                    type="button"
                    data-testid="cw-delete"
                    onClick={() => handleDelete(doc.id, doc.title)}
                    title={t("coWriter.deleteDoc")}
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
