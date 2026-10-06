"use client";

/**
 * 笔记本详情（§7.15 正式版）：记录列表（Markdown 渲染）+ 手写笔记 + 行内编辑 +
 * 移动/复制到其它本 + 整本导出 + 笔记本改名。
 *
 * `?record=<id>` 深链（引用 chip 点进来的入口）三种情形：
 * - 记录在本本 → 滚动定位并高亮；
 * - 在别的本 → 自动跳到它现在的归属（移动后 chip 仍指对地方）；
 * - 记录没了 → 顶部一条失效提示（宽容：引用画得像引用，没了也不拦路）。
 * 读了 useSearchParams，所以本体包在 Suspense 里（Next 16 的硬要求）。
 */

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Check,
  Download,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import Markdown from "@/components/chat/Markdown";
import { confirmDialog } from "@/hooks/useConfirm";
import {
  useAddRecord,
  useCopyRecord,
  useDeleteRecord,
  useNotebook,
  useNotebookList,
  useRecord,
  useUpdateNotebook,
  useUpdateRecord,
} from "@/hooks/useNotebooks";
import { useI18n } from "@/hooks/useI18n";
import { errorText } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { Language } from "@/i18n";
import type { NotebookRecordType } from "@/types/api";

const TYPE_LABEL_KEYS: Record<NotebookRecordType, string> = {
  note: "notebooks.typeNote",
  chat: "notebooks.typeChat",
  solve: "notebooks.typeSolve",
  question: "notebooks.typeQuestion",
  research: "notebooks.typeResearch",
  visualize: "notebooks.typeVisualize",
  math_animator: "notebooks.typeMathAnimator",
};

function dateLabel(seconds: number, lang: Language): string {
  return formatDateTime(seconds, lang);
}

export default function NotebookDetailPage() {
  return (
    <Suspense
      fallback={
        <main className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </main>
      }
    >
      <NotebookBoard />
    </Suspense>
  );
}

function NotebookBoard() {
  const { t, lang } = useI18n();
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const notebookId = params?.id ?? null;
  const recordParam = searchParams.get("record") ?? "";
  const { data, isLoading, error } = useNotebook(notebookId);
  // 深链记录查询：只用于「在别的本 → 跳过去 / 没了 → 提示」两个判断
  const recordRef = useRecord(recordParam || null);
  const { data: listData } = useNotebookList();
  const add = useAddRecord();
  const remove = useDeleteRecord();
  const update = useUpdateRecord();
  const copy = useCopyRecord();
  const updateNotebook = useUpdateNotebook();

  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [recordError, setRecordError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editBody, setEditBody] = useState("");
  const [menuId, setMenuId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameName, setRenameName] = useState("");
  const [renameDesc, setRenameDesc] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  // 失效提示的「已关掉」按 param 记：换一个引用深链时提示自然重新出现
  const [dismissedRef, setDismissedRef] = useState<string | null>(null);
  const menuRef = useRef<HTMLLIElement>(null);
  const scrolledRef = useRef(false);

  const records = data?.records ?? [];
  const notebooks = listData?.notebooks ?? [];
  const others = notebooks.filter((item) => item.id !== notebookId);
  const refGone = Boolean(recordParam) && recordRef.isError && dismissedRef !== recordParam;

  // 深链：记录在别的本 → 跳到它现在的归属（查不到的用 refGone 就地提示，不拦页面）
  useEffect(() => {
    if (!recordParam) {
      return;
    }
    const target = recordRef.data;
    if (target && notebookId && target.notebook_id !== notebookId) {
      router.replace(`/notebooks/${target.notebook_id}?record=${target.id}`);
    }
  }, [recordParam, recordRef.data, notebookId, router]);

  // 定位滚动：记录渲染完滚一次（之后失效刷新/编辑重取不再打扰）
  useEffect(() => {
    if (!recordParam || !data || scrolledRef.current) {
      return;
    }
    scrolledRef.current = true;
    document
      .querySelector(`[data-record-id="${recordParam}"]`)
      ?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [recordParam, data]);

  // 移动/复制弹层：点外面关掉
  useEffect(() => {
    if (!menuId) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuId(null);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuId]);

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
      setFormError(errorText(err, t("common.requestFailed")));
    }
  };

  const handleDelete = (recordId: string) => {
    if (!notebookId) {
      return;
    }
    void confirmDialog({ message: t("notebooks.deleteRecordConfirm"), danger: true }).then((ok) => {
      if (ok) {
        remove.mutate({ notebookId, recordId });
      }
    });
  };

  const startEdit = (recordId: string, recordTitle: string, recordBody: string) => {
    setRecordError(null);
    setEditingId(recordId);
    setEditTitle(recordTitle);
    setEditBody(recordBody);
  };

  const saveEdit = async (recordId: string) => {
    if (!notebookId) {
      return;
    }
    if (!editBody.trim()) {
      setRecordError(t("notebooks.bodyRequired"));
      return;
    }
    setRecordError(null);
    try {
      await update.mutateAsync({
        notebookId,
        recordId,
        title: editTitle,
        content_md: editBody,
      });
      setEditingId(null);
    } catch (err) {
      setRecordError(errorText(err, t("common.requestFailed")));
    }
  };

  const moveTo = async (recordId: string, targetId: string) => {
    if (!notebookId) {
      return;
    }
    setRecordError(null);
    try {
      await update.mutateAsync({ notebookId, recordId, targetNotebookId: targetId });
      setMenuId(null);
    } catch (err) {
      setRecordError(errorText(err, t("common.requestFailed")));
    }
  };

  const copyTo = async (recordId: string, targetId: string) => {
    if (!notebookId) {
      return;
    }
    setRecordError(null);
    try {
      await copy.mutateAsync({ notebookId, recordId, targetNotebookId: targetId });
      setMenuId(null);
    } catch (err) {
      setRecordError(errorText(err, t("common.requestFailed")));
    }
  };

  const startRename = () => {
    if (!data) {
      return;
    }
    setRenameError(null);
    setRenameName(data.name);
    setRenameDesc(data.description ?? "");
    setRenaming(true);
  };

  const saveRename = async () => {
    if (!notebookId) {
      return;
    }
    setRenameError(null);
    try {
      await updateNotebook.mutateAsync({
        id: notebookId,
        name: renameName,
        description: renameDesc,
      });
      setRenaming(false);
    } catch (err) {
      setRenameError(errorText(err, t("common.requestFailed")));
    }
  };

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <Link
          href="/notebooks"
          className="inline-flex w-fit items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {t("notebooks.backToList")}
        </Link>

        {refGone && (
          <div
            data-testid="nb-ref-gone"
            className="flex items-center justify-between gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger"
          >
            <span>{t("notebooks.refMissing")}</span>
            <button
              type="button"
              aria-label={t("common.close")}
              onClick={() => setDismissedRef(recordParam)}
              className="rounded p-0.5 transition-colors hover:bg-accent"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error || !data ? (
          <p className="text-sm text-danger">
            {error ? errorText(error, t("common.requestFailed")) : t("notebooks.notFound")}
          </p>
        ) : (
          <>
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                {renaming ? (
                  <div className="space-y-2">
                    <input
                      data-testid="nb-rename-name"
                      value={renameName}
                      onChange={(event) => setRenameName(event.target.value)}
                      className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
                    />
                    <input
                      data-testid="nb-rename-desc"
                      value={renameDesc}
                      onChange={(event) => setRenameDesc(event.target.value)}
                      placeholder={t("notebooks.descPlaceholder")}
                      className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-xs outline-none focus:border-primary/50"
                    />
                    {renameError && <p className="text-xs text-danger">{renameError}</p>}
                    <div className="flex gap-2">
                      <button
                        type="button"
                        data-testid="nb-rename-save"
                        onClick={() => void saveRename()}
                        disabled={updateNotebook.isPending}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
                      >
                        {t("common.save")}
                      </button>
                      <button
                        type="button"
                        onClick={() => setRenaming(false)}
                        className="rounded-lg px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
                      >
                        {t("common.cancel")}
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <h1 className="text-xl font-semibold">{data.name}</h1>
                    {data.description && (
                      <p className="mt-0.5 text-xs text-muted">{data.description}</p>
                    )}
                    <p className="mt-1 text-xs text-muted">
                      {t("notebooks.recordCount", { n: String(records.length) })}
                    </p>
                  </>
                )}
              </div>
              {!renaming && (
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    data-testid="nb-rename"
                    onClick={startRename}
                    title={t("notebooks.rename")}
                    className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <a
                    data-testid="nb-export"
                    href={`/api/v1/notebooks/${data.id}/export`}
                    download
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
                  >
                    <Download className="h-3.5 w-3.5" />
                    {t("notebooks.export")}
                  </a>
                </div>
              )}
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

            {recordError && <p className="text-xs text-danger">{recordError}</p>}

            {records.length === 0 ? (
              <p className="rounded-xl border border-dashed border-border py-10 text-center text-sm text-muted">
                {t("notebooks.noRecords")}
              </p>
            ) : (
              <ul className="space-y-3">
                {records.map((record) => {
                  const editing = editingId === record.id;
                  const highlighted = record.id === recordParam;
                  return (
                    <li
                      key={record.id}
                      ref={menuId === record.id ? menuRef : undefined}
                      data-testid="nb-record"
                      data-id={record.id}
                      data-record-id={record.id}
                      className={`rounded-xl border bg-surface p-4 transition-shadow ${
                        highlighted ? "border-primary/50 ring-1 ring-primary/40" : "border-border"
                      }`}
                    >
                      <div className="flex items-start gap-3">
                        <div className="min-w-0 flex-1">
                          {editing ? (
                            <input
                              data-testid="nb-edit-title"
                              value={editTitle}
                              onChange={(event) => setEditTitle(event.target.value)}
                              placeholder={t("notebooks.noteTitlePlaceholder")}
                              className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
                            />
                          ) : (
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="truncate text-sm font-medium">{record.title}</span>
                              <span
                                data-testid="nb-record-type"
                                data-type={record.type}
                                className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted"
                              >
                                {t(TYPE_LABEL_KEYS[record.type] ?? "notebooks.typeNote")}
                              </span>
                              <span className="shrink-0 text-[11px] text-muted">
                                {dateLabel(record.created_at, lang)}
                              </span>
                            </div>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          {editing ? (
                            <>
                              <button
                                type="button"
                                data-testid="nb-edit-save"
                                onClick={() => void saveEdit(record.id)}
                                disabled={update.isPending}
                                title={t("common.save")}
                                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
                              >
                                {update.isPending ? (
                                  <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                  <Check className="h-4 w-4" />
                                )}
                              </button>
                              <button
                                type="button"
                                data-testid="nb-edit-cancel"
                                onClick={() => setEditingId(null)}
                                title={t("common.cancel")}
                                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
                              >
                                <X className="h-4 w-4" />
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                data-testid="nb-record-edit"
                                onClick={() =>
                                  startEdit(record.id, record.title, record.content_md)
                                }
                                title={t("notebooks.editRecord")}
                                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
                              >
                                <Pencil className="h-4 w-4" />
                              </button>
                              <div className="relative">
                                <button
                                  type="button"
                                  data-testid="nb-record-menu"
                                  onClick={() => setMenuId(menuId === record.id ? null : record.id)}
                                  aria-expanded={menuId === record.id}
                                  title={t("notebooks.moveCopy")}
                                  className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
                                >
                                  <MoreHorizontal className="h-4 w-4" />
                                </button>
                                {menuId === record.id && (
                                  <div
                                    data-testid="nb-record-actions"
                                    className="absolute right-0 top-full z-50 mt-1 w-[240px] overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-lg"
                                  >
                                    <div className="px-3 py-1 text-[10px] font-medium text-muted">
                                      {t("notebooks.moveTo")}
                                    </div>
                                    {others.length === 0 ? (
                                      <div className="px-3 py-1.5 text-[11px] text-muted">
                                        {t("notebooks.noMoveTargets")}
                                      </div>
                                    ) : (
                                      others.map((notebook) => (
                                        <button
                                          key={notebook.id}
                                          type="button"
                                          data-testid="nb-move-target"
                                          data-id={notebook.id}
                                          disabled={update.isPending}
                                          onClick={() => void moveTo(record.id, notebook.id)}
                                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent disabled:opacity-50"
                                        >
                                          <span className="min-w-0 flex-1 truncate">
                                            {notebook.name}
                                          </span>
                                          <span className="shrink-0 text-[10px] text-muted">
                                            {notebook.record_count ?? 0}
                                          </span>
                                        </button>
                                      ))
                                    )}
                                    <div className="mt-1 border-t border-border px-3 py-1 text-[10px] font-medium text-muted">
                                      {t("notebooks.copyTo")}
                                    </div>
                                    {notebooks.map((notebook) => (
                                      <button
                                        key={notebook.id}
                                        type="button"
                                        data-testid="nb-copy-target"
                                        data-id={notebook.id}
                                        disabled={copy.isPending}
                                        onClick={() => void copyTo(record.id, notebook.id)}
                                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent disabled:opacity-50"
                                      >
                                        <span className="min-w-0 flex-1 truncate">
                                          {notebook.name}
                                        </span>
                                        <span className="shrink-0 text-[10px] text-muted">
                                          {notebook.record_count ?? 0}
                                        </span>
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                              <button
                                type="button"
                                onClick={() => handleDelete(record.id)}
                                title={t("notebooks.deleteRecord")}
                                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
                              >
                                <Trash2 className="h-4 w-4" />
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                      {editing ? (
                        <textarea
                          data-testid="nb-edit-body"
                          value={editBody}
                          onChange={(event) => setEditBody(event.target.value)}
                          rows={6}
                          className="mt-2 w-full resize-y rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
                        />
                      ) : (
                        <div className="mt-2 max-w-full">
                          <Markdown text={record.content_md} />
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        )}
      </div>
    </main>
  );
}
