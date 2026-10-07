"use client";

/**
 * 活书控制台（§7.14）：按状态分派四种视图——
 * draft：目录编辑器 + 估算 + 开始编译；compiling/paused：编译看板（轮询）+ 暂停/继续；
 * ready：健康横幅 + 进度 + 目录 + 导出/开始阅读；error：出错信息 + 重试。
 *
 * 结构照 co-writer 工作台：外壳取数 + 内层 keyed（换书强制重挂载，目录编辑的本地
 * 状态只在挂载时初始化一次，不在 effect 里 setState）。目录编辑每次提交都是**全量
 * 有序列表**（服务端靠数组顺序认排序），PATCH 期间控件禁用，避免并发 PATCH 互相覆盖。
 */

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Download, Loader2, Play, RotateCcw } from "lucide-react";
import ChapterToc from "@/components/book/ChapterToc";
import CompileBoard from "@/components/book/CompileBoard";
import EstimatePanel from "@/components/book/EstimatePanel";
import HealthBanner from "@/components/book/HealthBanner";
import ProgressRing from "@/components/book/ProgressRing";
import SpineEditor from "@/components/book/SpineEditor";
import { confirmDialog } from "@/hooks/useConfirm";
import { useBookDetail, useCompileBook, usePauseCompile, useUpdateBook } from "@/hooks/useBook";
import { useI18n } from "@/hooks/useI18n";
import { pageOfChapter } from "@/lib/book";
import { errorText } from "@/lib/errors";
import type { BookChapter, BookDetail } from "@/types/api";

export default function BookConsolePage() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const id = params?.id ?? "";
  const { data, isLoading, error } = useBookDetail(id || null);

  if (!id || (!isLoading && (error || !data))) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-3">
        <p className="text-sm text-muted">{t("book.notFound")}</p>
        <Link
          href="/book"
          className="inline-flex items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {t("book.backToList")}
        </Link>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </main>
    );
  }

  return <BookConsole key={data.id} book={data} />;
}

function BookConsole({ book }: { book: BookDetail }) {
  const { t } = useI18n();
  const update = useUpdateBook();
  const compile = useCompileBook();
  const pause = usePauseCompile();

  // 目录编辑先落本地（含改名草稿），提交后再等服务端响应刷新估算
  const [chapters, setChapters] = useState<BookChapter[]>(() => book.spine.chapters);
  const [title, setTitle] = useState(book.title);
  const [actionError, setActionError] = useState<string | null>(null);

  const commitChapters = (next: BookChapter[]) => {
    setChapters(next);
    update.mutate(
      { id: book.id, chapters: next.map((item) => ({ key: item.key, title: item.title })) },
      {
        onError: (err) => {
          setActionError(errorText(err, t("common.requestFailed")));
          setChapters(book.spine.chapters); // 退回服务端那份，别让本地飘着
        },
      }
    );
  };

  const commitTitle = () => {
    const value = title.trim();
    if (!value || value === book.title) {
      setTitle(book.title);
      return;
    }
    update.mutate(
      { id: book.id, title: value },
      { onError: (err) => setActionError(errorText(err, t("common.requestFailed"))) }
    );
  };

  const startCompile = () => {
    setActionError(null);
    compile.mutate(
      { id: book.id },
      { onError: (err) => setActionError(errorText(err, t("common.requestFailed"))) }
    );
  };

  const recompileChapter = (chapterKey: string) => {
    const chapter = chapters.find((item) => item.key === chapterKey);
    void confirmDialog({
      message: t("book.recompileChapterConfirm", { title: chapter?.title ?? chapterKey }),
      danger: false,
    }).then((ok) => {
      if (ok) {
        setActionError(null);
        compile.mutate(
          { id: book.id, chapters: [chapterKey] },
          { onError: (err) => setActionError(errorText(err, t("common.requestFailed"))) }
        );
      }
    });
  };

  const firstPage = book.spine.chapters.length
    ? pageOfChapter(book.pages, book.spine.chapters[0].key)
    : null;
  const stats = book.progress;

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-center gap-3">
          <Link
            href="/book"
            title={t("book.backToList")}
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <input
            data-testid="bk-book-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onBlur={commitTitle}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.currentTarget.blur();
              }
            }}
            aria-label={t("book.formTitle")}
            className="min-w-0 flex-1 bg-transparent text-lg font-semibold outline-none"
          />
          {book.status === "ready" && firstPage && (
            <Link
              href={`/book/${book.id}/${firstPage.id}`}
              data-testid="bk-read"
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              {t("book.read")}
            </Link>
          )}
          <a
            data-testid="bk-export"
            href={`/api/v1/books/${book.id}/export`}
            download
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <Download className="h-3.5 w-3.5" />
            {t("book.export")}
          </a>
        </div>

        {book.issues.length > 0 && (
          <div data-testid="bk-issues" className="rounded-xl border border-border bg-surface p-3">
            <div className="flex items-center gap-2 text-xs font-medium">
              <AlertTriangle className="h-3.5 w-3.5 text-muted" />
              {t("book.issuesTitle")}
            </div>
            <ul className="mt-1.5 space-y-0.5">
              {book.issues.map((issue, index) => (
                <li key={index} className="text-xs text-muted">
                  {issue}
                </li>
              ))}
            </ul>
          </div>
        )}

        {actionError && (
          <p data-testid="bk-error" className="text-xs text-danger">
            {actionError}
          </p>
        )}

        {book.status === "draft" && (
          <>
            <div className="rounded-xl border border-border bg-surface p-4">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-medium">{t("book.spineTitle")}</h2>
                <span className="text-xs text-muted">{t("book.spineHint")}</span>
              </div>
              <div className="mt-3">
                <SpineEditor
                  chapters={chapters}
                  busy={update.isPending}
                  disabled={update.isPending}
                  onCommit={commitChapters}
                />
              </div>
            </div>
            <EstimatePanel estimate={book.estimate} />
            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="bk-compile-start"
                disabled={compile.isPending}
                onClick={startCompile}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
              >
                {compile.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Play className="h-3.5 w-3.5" />
                )}
                {compile.isPending ? t("book.compiling") : t("book.startCompile")}
              </button>
              <span className="text-xs text-muted">{t("book.compileStartHint")}</span>
            </div>
          </>
        )}

        {(book.status === "compiling" || book.status === "paused") && (
          <CompileBoard
            spine={book.spine}
            pages={book.pages}
            status={book.status}
            busy={pause.isPending || compile.isPending}
            onPause={() =>
              pause.mutate(book.id, {
                onError: (err) => setActionError(errorText(err, t("common.requestFailed"))),
              })
            }
            onResume={startCompile}
          />
        )}

        {book.status === "error" && (
          <div
            data-testid="bk-compile-error"
            className="rounded-xl border border-danger/40 bg-surface p-4"
          >
            <p className="text-sm">{t("book.compileError", { message: book.error || "?" })}</p>
            <button
              type="button"
              data-testid="bk-compile-retry"
              disabled={compile.isPending}
              onClick={startCompile}
              className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              {t("book.retryCompile")}
            </button>
          </div>
        )}

        {book.status === "ready" && (
          <>
            <HealthBanner
              spine={book.spine}
              health={book.health}
              busy={compile.isPending}
              onRecompileChapter={recompileChapter}
            />
            <div className="flex items-center gap-4 rounded-xl border border-border bg-surface p-4">
              <ProgressRing percent={stats.completion * 100} size={52} />
              <div className="min-w-0 flex-1 text-xs text-muted">
                <div data-testid="bk-progress-pages">
                  {t("book.progressPages", {
                    done: String(stats.completed),
                    total: String(stats.pages),
                  })}
                </div>
                <div className="mt-1">
                  {stats.quizzes.answered > 0
                    ? t("book.progressQuiz", {
                        correct: String(stats.quizzes.correct),
                        answered: String(stats.quizzes.answered),
                      })
                    : t("book.progressQuizEmpty")}
                </div>
              </div>
            </div>
            <ChapterToc
              bookId={book.id}
              spine={book.spine}
              pages={book.pages}
              busy={compile.isPending}
              onRecompileChapter={recompileChapter}
            />
          </>
        )}
      </div>
    </main>
  );
}
