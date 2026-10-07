"use client";

/**
 * 活书阅读器（§7.14）：左章节目录 + 中正文块流 + 右页聊天。
 *
 * 结构照控制台：外壳取数 + `<BookReader key={page.id}>`（换页强制重挂载，块编辑/
 * 草稿这些本地状态只在挂载时初始化一次）。进页即置 visited（每个页 id 只发一次，
 * 用 ref 兜住重复触发）；翻页/答完/聊完都靠失效前缀刷新——页面数据只有服务端
 * 一份，本地不镜像块数组。
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ArrowLeft,
  Bookmark,
  BookmarkCheck,
  Check,
  ChevronLeft,
  ChevronRight,
  Loader2,
} from "lucide-react";
import BlockRenderer, { type BlockActions } from "@/components/book/BlockRenderer";
import PageChatPanel from "@/components/book/PageChatPanel";
import { confirmDialog } from "@/hooks/useConfirm";
import { useBookDetail, useBookPage, useEditBlocks, usePageFlags } from "@/hooks/useBook";
import { useI18n } from "@/hooks/useI18n";
import { chapterRows, isMarkdownBlock, markdownOf } from "@/lib/book";
import { errorText } from "@/lib/errors";
import type {
  BlockType,
  BookBlock,
  BookBlockEditBody,
  BookDetail,
  BookPageDetail,
  BookPageMeta,
} from "@/types/api";

export default function BookReaderPage() {
  const { t } = useI18n();
  const params = useParams<{ id: string; page: string }>();
  const bookId = params?.id ?? "";
  const pageId = params?.page ?? "";
  const book = useBookDetail(bookId || null);
  const page = useBookPage(bookId || null, pageId || null);

  // 拿不到数据才算「页没了」：轮询/重取失败但手上有旧数据时继续显示旧页
  if (!book.data || !page.data) {
    const gone =
      !bookId ||
      !pageId ||
      (!book.isLoading && (book.error || !book.data)) ||
      (!page.isLoading && (page.error || !page.data));
    if (gone) {
      return (
        <main className="flex flex-1 flex-col items-center justify-center gap-3">
          <p className="text-sm text-muted">{t("book.pageNotFound")}</p>
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
    return (
      <main className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </main>
    );
  }

  return <BookReader key={page.data.id} book={book.data} page={page.data} />;
}

function BookReader({ book, page }: { book: BookDetail; page: BookPageDetail }) {
  const { t } = useI18n();
  const edit = useEditBlocks();
  const flags = usePageFlags(book.id, page.id);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const visitedOnce = useRef<string | null>(null);

  // 进页记一次已读（同页只发一次；flags 对象每渲染都新，靠 ref 兜重复触发）
  useEffect(() => {
    if (page.visited || visitedOnce.current === page.id) {
      return;
    }
    visitedOnce.current = page.id;
    flags.mutate({ visited: true });
  }, [flags, page.id, page.visited]);

  const busyBlockId = edit.isPending ? edit.variables?.block_id ?? null : null;
  const rows = chapterRows(book.spine, book.pages);
  const ordered: BookPageMeta[] = rows.flatMap((row) => (row.page ? [row.page] : []));
  const position = ordered.findIndex((item) => item.id === page.id);
  const prev = position > 0 ? ordered[position - 1] : null;
  const next = position >= 0 && position < ordered.length - 1 ? ordered[position + 1] : null;
  const chapterIndex = rows.findIndex((row) => row.chapter.key === page.chapter_key);

  const runEdit = (body: BookBlockEditBody) => {
    setError(null);
    edit.mutate(
      { bookId: book.id, pageId: page.id, ...body },
      {
        onError: (err) => setError(errorText(err, t("common.requestFailed"))),
        onSuccess: () => {
          // 保存成功（update）即收起草稿框——不然编辑态挂在那不走（E2E ⑤ 抓到过）；
          // 正文刷新靠 mutation 失效重取，本地不镜像，短暂显示旧文随后即更新
          if (body.op === "update") {
            setEditingId(null);
            setDraft("");
          }
        },
      }
    );
  };

  const handleInsert = async (afterId: string, type: BlockType) => {
    setError(null);
    try {
      const result = await edit.mutateAsync({
        bookId: book.id,
        pageId: page.id,
        op: "insert",
        type,
        after_block_id: afterId,
      });
      if (!("blocks" in result)) {
        return;
      }
      const known = new Set(page.blocks.map((item) => item.id));
      const created = result.blocks.find((item) => !known.has(item.id));
      if (!created) {
        return;
      }
      if (created.type === "note") {
        // 手写块建好即成稿：直接进编辑态，用户敲完点保存
        setEditingId(created.id);
        setDraft("");
        return;
      }
      // 其余类型建好是 pending：接着生成（一次「插入」= 要一块能看的内容）
      await edit.mutateAsync({
        bookId: book.id,
        pageId: page.id,
        op: "regenerate",
        block_id: created.id,
      });
    } catch (err) {
      setError(errorText(err, t("common.requestFailed")));
    }
  };

  const handleRegenerate = (block: BookBlock) => {
    if (block.status !== "done") {
      runEdit({ op: "regenerate", block_id: block.id });
      return;
    }
    void confirmDialog({ message: t("book.regenerateConfirm"), danger: false }).then((ok) => {
      if (ok) {
        runEdit({ op: "regenerate", block_id: block.id });
      }
    });
  };

  const handleRetype = (block: BookBlock, type: BlockType) => {
    if (type === block.type) {
      return;
    }
    void confirmDialog({
      message: t("book.retypeConfirm", { type: t(`book.blockType_${type}`) }),
      danger: false,
    }).then((ok) => {
      if (ok) {
        setEditingId(null);
        runEdit({ op: "retype", block_id: block.id, type });
      }
    });
  };

  const handleDelete = (block: BookBlock) => {
    void confirmDialog({ message: t("book.deleteBlockConfirm"), danger: true }).then((ok) => {
      if (ok) {
        setEditingId(null);
        runEdit({ op: "delete", block_id: block.id });
      }
    });
  };

  const actionsFor = (block: BookBlock, index: number): BlockActions => ({
    index,
    total: page.blocks.length,
    busy: edit.isPending,
    busyHere: busyBlockId === block.id,
    editing: editingId === block.id,
    draft,
    canEdit: isMarkdownBlock(block.type),
    onMove: (direction) => runEdit({ op: "move", block_id: block.id, direction }),
    onInsert: (type) => void handleInsert(block.id, type),
    onRegenerate: () => handleRegenerate(block),
    onRetype: (type) => handleRetype(block, type),
    onDelete: () => handleDelete(block),
    onStartEdit: () => {
      setDraft(markdownOf(block.payload));
      setEditingId(block.id);
    },
    onChangeDraft: setDraft,
    onSave: () =>
      runEdit({ op: "update", block_id: block.id, payload: { ...block.payload, markdown: draft } }),
    onCancelEdit: () => {
      setEditingId(null);
      setDraft("");
    },
  });

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-6xl justify-center gap-6 px-4 py-6">
        <aside className="hidden w-52 shrink-0 lg:block">
          <div className="sticky top-2 space-y-0.5">
            <p className="px-2 pb-1 text-xs font-medium text-muted">{t("book.tocTitle")}</p>
            {rows.map((row, index) => {
              const current = row.chapter.key === page.chapter_key;
              return (
                <Link
                  key={row.chapter.key}
                  href={row.page ? `/book/${book.id}/${row.page.id}` : "#"}
                  data-testid="bk-reader-toc-row"
                  data-current={current ? "true" : "false"}
                  className={`flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs transition-colors ${
                    current
                      ? "bg-accent text-foreground"
                      : "text-muted hover:bg-accent hover:text-foreground"
                  }`}
                >
                  <span className="w-4 shrink-0 text-[10px] text-muted">{index + 1}</span>
                  <span className="min-w-0 flex-1 truncate">{row.chapter.title}</span>
                  {row.page?.bookmarked && (
                    <Bookmark aria-hidden className="h-3 w-3 shrink-0 text-primary" />
                  )}
                  {row.page?.visited && (
                    <Check aria-hidden className="h-3 w-3 shrink-0 text-success" />
                  )}
                </Link>
              );
            })}
          </div>
        </aside>

        <article className="min-w-0 max-w-3xl flex-1">
          <div className="flex items-center gap-2">
            <Link
              href={`/book/${book.id}`}
              title={t("book.backToConsole")}
              className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
            >
              <ArrowLeft className="h-4 w-4" />
            </Link>
            <span className="min-w-0 flex-1 truncate text-xs text-muted">{book.title}</span>
            <button
              type="button"
              data-testid="bk-bookmark"
              data-on={page.bookmarked ? "true" : "false"}
              onClick={() => flags.mutate({ bookmarked: !page.bookmarked })}
              className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition-colors ${
                page.bookmarked
                  ? "border-primary/40 text-primary"
                  : "border-border text-muted hover:bg-accent hover:text-foreground"
              }`}
            >
              {page.bookmarked ? (
                <BookmarkCheck className="h-3.5 w-3.5" />
              ) : (
                <Bookmark className="h-3.5 w-3.5" />
              )}
              {page.bookmarked ? t("book.bookmarked") : t("book.bookmark")}
            </button>
          </div>

          <header className="mt-4">
            <p className="text-xs text-muted">
              {t("book.chapterOf", {
                i: String(chapterIndex >= 0 ? chapterIndex + 1 : position + 1),
                n: String(rows.length),
              })}
            </p>
            <h1 className="mt-1 text-xl font-semibold">{page.chapter.title}</h1>
            {page.chapter.summary && (
              <p className="mt-1.5 text-sm leading-relaxed text-muted">{page.chapter.summary}</p>
            )}
            {page.chapter.objectives.length > 0 && (
              <ul className="mt-2 space-y-0.5">
                {page.chapter.objectives.map((objective, index) => (
                  <li key={index} className="flex gap-1.5 text-xs text-muted">
                    <span aria-hidden className="text-primary">
                      ·
                    </span>
                    <span className="min-w-0 flex-1">{objective}</span>
                  </li>
                ))}
              </ul>
            )}
          </header>

          {error && (
            <p data-testid="bk-reader-error" className="mt-3 text-xs text-danger">
              {error}
            </p>
          )}

          <div className="mt-5 space-y-6">
            {page.blocks.map((block, index) => (
              <BlockRenderer
                key={block.id}
                block={block}
                bookId={book.id}
                pageId={page.id}
                attempt={page.attempts.find((item) => item.block_id === block.id) ?? null}
                actions={actionsFor(block, index)}
              />
            ))}
            {page.blocks.length === 0 && (
              <p className="py-10 text-center text-sm text-muted">{t("book.pageNoBlocks")}</p>
            )}
          </div>

          <nav className="mt-8 flex items-center justify-between border-t border-border pt-4">
            {prev ? (
              <Link
                href={`/book/${book.id}/${prev.id}`}
                data-testid="bk-prev"
                className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
                {t("book.prevPage")}
              </Link>
            ) : (
              <span />
            )}
            {next ? (
              <Link
                href={`/book/${book.id}/${next.id}`}
                data-testid="bk-next"
                className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
              >
                {t("book.nextPage")}
                <ChevronRight className="h-3.5 w-3.5" />
              </Link>
            ) : (
              <span />
            )}
          </nav>
        </article>

        <aside className="sticky top-2 hidden h-[calc(100vh-6rem)] w-80 shrink-0 xl:flex xl:flex-col">
          <PageChatPanel
            bookId={book.id}
            pageId={page.id}
            chapterTitle={page.chapter.title}
            messages={page.messages}
          />
        </aside>
      </div>
    </main>
  );
}
