"use client";

/** 「+」引用菜单（§7.1 / §7.21）：挑一条笔记本记录或一道题，随下一条消息一次性注入。
 *
 * 两个 tab：
 * - 笔记本：先选本、再看记录（两跳，返回键回上一层）；
 * - 题库：直接列题，默认「错题」——验收主场景就是错题引用进去接着问，
 *   少点一步；切「全部」看全库。
 *
 * 选中进 store 的 pendingRefs（Composer 上方的 chips），再点一次取消；发消息时
 * 随载荷下发并清空。查询复用题库/笔记本的既有 hooks（同一份 TanStack 缓存，
 * 和页面之间不重复拉）。 */

import { useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, Check, ChevronLeft, ListChecks, Loader2, Plus } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { useNotebook, useNotebookList } from "@/hooks/useNotebooks";
import { useQuestionList } from "@/hooks/useQuestions";
import { questionRefLabel, refKey, type PendingRef } from "@/lib/refs";

type Tab = "notebook" | "question";

export default function ReferenceMenu() {
  const { t } = useI18n();
  const pendingRefs = useChatStore((s) => s.pendingRefs);
  const addPendingRef = useChatStore((s) => s.addPendingRef);
  const removePendingRef = useChatStore((s) => s.removePendingRef);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("notebook");
  const [notebookId, setNotebookId] = useState<string | null>(null);
  const [wrongOnly, setWrongOnly] = useState(true);
  const rootRef = useRef<HTMLDivElement>(null);

  const notebooks = useNotebookList();
  const notebook = useNotebook(open && tab === "notebook" ? notebookId : null);
  const questions = useQuestionList({
    filter: wrongOnly ? "wrong" : "all",
    enabled: open && tab === "question",
  });

  useEffect(() => {
    if (!open) {
      return;
    }
    const onMouseDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const selectedKeys = useMemo(() => new Set(pendingRefs.map(refKey)), [pendingRefs]);

  /** 点已选中的条目 = 取消（菜单不关，可以连着挑几条）。 */
  const toggle = (ref: PendingRef) => {
    if (selectedKeys.has(refKey(ref))) {
      removePendingRef(ref);
    } else {
      addPendingRef(ref);
    }
  };

  const counts = questions.data?.counts;
  const filterChip = (active: boolean) =>
    `rounded-full px-2 py-0.5 text-[11px] transition-colors ${
      active ? "bg-primary text-primary-foreground" : "bg-accent/50 text-muted hover:bg-accent"
    }`;

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="ref-menu-btn"
        data-count={pendingRefs.length}
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="relative rounded-lg p-2 text-muted transition-colors hover:bg-accent hover:text-foreground"
        title={t("chat.refMenu")}
        aria-label={t("chat.reference")}
      >
        <Plus className="h-4 w-4" />
        {pendingRefs.length > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-primary px-0.5 text-[9px] leading-none text-primary-foreground">
            {pendingRefs.length}
          </span>
        )}
      </button>

      {open && (
        <div
          data-testid="ref-menu"
          className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(360px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
        >
          <div className="flex border-b border-border">
            {(["notebook", "question"] as Tab[]).map((value) => (
              <button
                key={value}
                type="button"
                data-testid={`ref-tab-${value}`}
                data-active={tab === value}
                onClick={() => setTab(value)}
                className={`flex-1 px-3 py-2 text-xs transition-colors ${
                  tab === value
                    ? "bg-accent/60 font-medium text-foreground"
                    : "text-muted hover:bg-accent/40"
                }`}
              >
                {t(value === "notebook" ? "chat.refTabNotebook" : "chat.refTabQuestion")}
              </button>
            ))}
          </div>

          <div className="max-h-[260px] overflow-y-auto py-1">
            {tab === "notebook" ? (
              notebookId === null ? (
                notebooks.isLoading ? (
                  <div className="flex justify-center py-4">
                    <Loader2 className="h-4 w-4 animate-spin text-muted" />
                  </div>
                ) : (notebooks.data?.notebooks.length ?? 0) === 0 ? (
                  <div className="px-3 py-2 text-[11px] text-muted">
                    {t("notebooks.noNotebooks")}
                  </div>
                ) : (
                  notebooks.data?.notebooks.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      data-testid="ref-notebook"
                      data-id={item.id}
                      onClick={() => setNotebookId(item.id)}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent"
                    >
                      <BookOpen className="h-3.5 w-3.5 shrink-0 text-muted" />
                      <span className="min-w-0 flex-1 truncate">{item.name}</span>
                      <span className="shrink-0 text-[10px] text-muted">
                        {t("notebooks.recordCount", { n: String(item.record_count ?? 0) })}
                      </span>
                    </button>
                  ))
                )
              ) : (
                <>
                  <div className="flex items-center gap-1 px-2 pb-1">
                    <button
                      type="button"
                      data-testid="ref-back"
                      onClick={() => setNotebookId(null)}
                      className="inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-[11px] text-muted transition-colors hover:bg-accent hover:text-foreground"
                    >
                      <ChevronLeft className="h-3 w-3" />
                      {t("chat.refBack")}
                    </button>
                    <span className="min-w-0 flex-1 truncate text-[11px] text-muted">
                      {notebook.data?.name ?? ""}
                    </span>
                  </div>
                  {notebook.isLoading ? (
                    <div className="flex justify-center py-4">
                      <Loader2 className="h-4 w-4 animate-spin text-muted" />
                    </div>
                  ) : (notebook.data?.records?.length ?? 0) === 0 ? (
                    <div className="px-3 py-2 text-[11px] text-muted">{t("chat.refNoRecords")}</div>
                  ) : (
                    notebook.data?.records?.map((record) => {
                      const ref: PendingRef = {
                        kind: "notebook_record",
                        notebook_id: notebookId,
                        record_id: record.id,
                        label: record.title,
                      };
                      return (
                        <button
                          key={record.id}
                          type="button"
                          data-testid="ref-record"
                          data-id={record.id}
                          onClick={() => toggle(ref)}
                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent"
                        >
                          <BookOpen className="h-3.5 w-3.5 shrink-0 text-muted" />
                          <span className="min-w-0 flex-1 truncate">{record.title}</span>
                          {selectedKeys.has(refKey(ref)) && (
                            <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                          )}
                        </button>
                      );
                    })
                  )}
                </>
              )
            ) : (
              <>
                <div className="flex items-center gap-1 px-2 pb-1">
                  <button
                    type="button"
                    data-testid="ref-q-all"
                    data-active={!wrongOnly}
                    onClick={() => setWrongOnly(false)}
                    className={filterChip(!wrongOnly)}
                  >
                    {t("questions.filterAll")}
                    {counts ? ` · ${counts.all}` : ""}
                  </button>
                  <button
                    type="button"
                    data-testid="ref-q-wrong"
                    data-active={wrongOnly}
                    onClick={() => setWrongOnly(true)}
                    className={filterChip(wrongOnly)}
                  >
                    {t("questions.filterWrong")}
                    {counts ? ` · ${counts.wrong}` : ""}
                  </button>
                </div>
                {questions.isLoading ? (
                  <div className="flex justify-center py-4">
                    <Loader2 className="h-4 w-4 animate-spin text-muted" />
                  </div>
                ) : (questions.data?.questions.length ?? 0) === 0 ? (
                  <div className="px-3 py-2 text-[11px] text-muted">{t("questions.empty")}</div>
                ) : (
                  questions.data?.questions.map((question) => {
                    const ref: PendingRef = {
                      kind: "question",
                      question_id: question.id,
                      label: questionRefLabel(question.stem),
                    };
                    return (
                      <button
                        key={question.id}
                        type="button"
                        data-testid="ref-question"
                        data-id={question.id}
                        onClick={() => toggle(ref)}
                        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-accent"
                      >
                        <ListChecks className="h-3.5 w-3.5 shrink-0 text-muted" />
                        <span className="min-w-0 flex-1 truncate">
                          {questionRefLabel(question.stem)}
                        </span>
                        {selectedKeys.has(refKey(ref)) && (
                          <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                        )}
                      </button>
                    );
                  })
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
