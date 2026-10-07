"use client";

/**
 * 活书书库（§7.14）：卡片 = 书名 + 状态 + 章节数 + 阅读进度环，可新建/删除。
 *
 * 新建会**同步**跑一次目录生成（一次模型调用，几十秒）：按钮转圈等结果，
 * 成功直接进控制台确认目录（建书的目的就是编它）。
 */

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { BookOpen, Loader2, Plus, Trash2 } from "lucide-react";
import BookStatusChip from "@/components/book/BookStatusChip";
import CreateBookForm from "@/components/book/CreateBookForm";
import ProgressRing from "@/components/book/ProgressRing";
import { confirmDialog } from "@/hooks/useConfirm";
import { useBookList, useCreateBook, useDeleteBook } from "@/hooks/useBook";
import { useI18n } from "@/hooks/useI18n";
import { sourceSummary } from "@/lib/book";
import { errorText } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { BookSources, BookSummary } from "@/types/api";

/** 素材摘要一行：「知识库 1 · 笔记本 2 · 题库全部」按选中的类拼。 */
function sourceText(book: BookSummary, t: (key: string, vars?: Record<string, string>) => string) {
  const summary = sourceSummary(book.sources);
  const parts: string[] = [];
  if (summary.kbs > 0) {
    parts.push(t("book.srcKb", { n: String(summary.kbs) }));
  }
  if (summary.notebooks > 0) {
    parts.push(t("book.srcNotebook", { n: String(summary.notebooks) }));
  }
  if (summary.sessions > 0) {
    parts.push(t("book.srcSession", { n: String(summary.sessions) }));
  }
  if (summary.questions === "all" || summary.questions === "wrong") {
    parts.push(t(`book.questionMode_${summary.questions}`));
  } else if (summary.questions === "ids") {
    parts.push(t("book.srcQuestionIds", { n: String(summary.questionIds) }));
  }
  return parts.join(" · ");
}

export default function BookLibraryPage() {
  const { t, lang } = useI18n();
  const router = useRouter();
  const { data, isLoading, error } = useBookList();
  const create = useCreateBook();
  const remove = useDeleteBook();
  const [formOpen, setFormOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const books = data?.books ?? [];

  const submit = async (input: { title: string; sources: BookSources }) => {
    setFormError(null);
    try {
      const book = await create.mutateAsync({ ...input, language: lang });
      setFormOpen(false);
      router.push(`/book/${book.id}`);
    } catch (err) {
      setFormError(
        err instanceof DOMException && err.name === "TimeoutError"
          ? t("book.createTimeout")
          : errorText(err, t("common.requestFailed"))
      );
    }
  };

  const handleDelete = (id: string, bookTitle: string) => {
    void confirmDialog({
      message: t("book.deleteConfirm", { title: bookTitle }),
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
            <h1 className="text-xl font-semibold">{t("book.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("book.desc")}</p>
          </div>
          {!formOpen && (
            <button
              type="button"
              data-testid="bk-new"
              onClick={() => setFormOpen(true)}
              className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("book.newBook")}
            </button>
          )}
        </div>

        {formOpen && (
          <CreateBookForm
            busy={create.isPending}
            error={formError}
            onSubmit={(input) => void submit(input)}
          />
        )}

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
        ) : books.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
            <BookOpen className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("book.empty")}</p>
            <p className="max-w-xs text-xs text-muted">{t("book.emptyHint")}</p>
          </div>
        ) : (
          <ul className="space-y-2">
            {books.map((book) => {
              const sources = sourceText(book, t);
              return (
                <li
                  key={book.id}
                  data-testid="bk-card"
                  data-id={book.id}
                  data-status={book.status}
                  className="rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40"
                >
                  <div className="flex items-start gap-3">
                    <Link href={`/book/${book.id}`} className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{book.title}</span>
                        <BookStatusChip status={book.status} />
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-muted">
                        <span>{t("book.chapters", { n: String(book.chapter_count) })}</span>
                        {sources && <span>· {sources}</span>}
                        <span>
                          · {t("book.updatedAt", { time: formatDateTime(book.updated_at, lang) })}
                        </span>
                      </div>
                    </Link>
                    {book.progress.pages > 0 && (
                      <ProgressRing
                        percent={book.progress.completion * 100}
                        label={t("book.progressPages", {
                          done: String(book.progress.completed),
                          total: String(book.progress.pages),
                        })}
                      />
                    )}
                    <button
                      type="button"
                      data-testid="bk-delete"
                      onClick={() => handleDelete(book.id, book.title)}
                      title={t("book.deleteBook")}
                      className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
