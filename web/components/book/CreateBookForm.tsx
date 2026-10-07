"use client";

/** 新建活书表单：书名 + 素材多选（知识库 / 笔记本 / 聊天会话 / 题库口径）。
 *
 * 至少选一类来源才放行（后端也这么校验）；没就绪的知识库禁选——建书时会同步
 * 跑一次目录生成，没就绪的库这时什么也供不上，选了只会让「素材不可用」白跑。
 */

import { useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookPlus, Loader2 } from "lucide-react";
import { KbStatusChip } from "@/components/knowledge/status";
import { useI18n } from "@/hooks/useI18n";
import { useKbList } from "@/hooks/useKnowledge";
import { useNotebookList } from "@/hooks/useNotebooks";
import { useQuestionList } from "@/hooks/useQuestions";
import { apiFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { SessionMeta } from "@/hooks/useChat";
import type { BookSources } from "@/types/api";

const QUESTION_MODES = ["none", "all", "wrong"] as const;
type QuestionMode = (typeof QUESTION_MODES)[number];

interface Props {
  busy: boolean;
  error: string | null;
  onSubmit: (input: { title: string; sources: BookSources }) => void;
}

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium">{title}</span>
        {hint && <span className="text-[11px] text-muted">{hint}</span>}
      </div>
      <div className="mt-2 space-y-1">{children}</div>
    </div>
  );
}

export default function CreateBookForm({ busy, error, onSubmit }: Props) {
  const { t, lang } = useI18n();
  const kbs = useKbList();
  const notebooks = useNotebookList();
  const sessions = useQuery({
    queryKey: ["sessions", "options"],
    queryFn: () => apiFetch<{ sessions: SessionMeta[] }>("/api/v1/sessions"),
  });
  const questions = useQuestionList({ filter: "all" });

  const [title, setTitle] = useState("");
  const [kbIds, setKbIds] = useState<string[]>([]);
  const [notebookIds, setNotebookIds] = useState<string[]>([]);
  const [sessionIds, setSessionIds] = useState<string[]>([]);
  const [questionMode, setQuestionMode] = useState<QuestionMode>("none");

  const toggle = (setter: Dispatch<SetStateAction<string[]>>, id: string) =>
    setter((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));

  const hasSources =
    kbIds.length > 0 || notebookIds.length > 0 || sessionIds.length > 0 || questionMode !== "none";
  const ready = title.trim().length > 0 && hasSources && !busy;

  const submit = () => {
    if (!ready) {
      return;
    }
    onSubmit({
      title: title.trim(),
      sources: {
        kbs: kbIds,
        notebooks: notebookIds,
        sessions: sessionIds,
        questions: questionMode === "none" ? null : { filter: questionMode, ids: [] },
      },
    });
  };

  const kbList = kbs.data?.kbs ?? [];
  const notebookList = notebooks.data?.notebooks ?? [];
  const sessionList = sessions.data?.sessions ?? [];
  const counts = questions.data?.counts;

  return (
    <div
      data-testid="bk-create-form"
      className="space-y-3 rounded-xl border border-border bg-surface p-4"
    >
      <input
        data-testid="bk-title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        placeholder={t("book.titlePlaceholder")}
        className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
      />

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Section title={t("book.secKb")} hint={t("book.secHint")}>
          {kbList.length === 0 ? (
            <p className="text-xs text-muted">{t("book.secEmpty")}</p>
          ) : (
            kbList.map((kb) => {
              const disabled = busy || kb.status !== "ready";
              return (
                <label
                  key={kb.id}
                  data-testid="bk-src-kb"
                  data-id={kb.id}
                  className={`flex items-center gap-2 text-xs ${
                    disabled ? "opacity-60" : "cursor-pointer"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={kbIds.includes(kb.id)}
                    disabled={disabled}
                    onChange={() => toggle(setKbIds, kb.id)}
                    className="h-3.5 w-3.5 accent-[var(--primary)]"
                  />
                  <span className="min-w-0 flex-1 truncate">{kb.name}</span>
                  <KbStatusChip status={kb.status} />
                </label>
              );
            })
          )}
        </Section>

        <Section title={t("book.secNotebooks")} hint={t("book.secHint")}>
          {notebookList.length === 0 ? (
            <p className="text-xs text-muted">{t("book.secEmpty")}</p>
          ) : (
            notebookList.map((notebook) => (
              <label
                key={notebook.id}
                data-testid="bk-src-notebook"
                data-id={notebook.id}
                className="flex cursor-pointer items-center gap-2 text-xs"
              >
                <input
                  type="checkbox"
                  checked={notebookIds.includes(notebook.id)}
                  disabled={busy}
                  onChange={() => toggle(setNotebookIds, notebook.id)}
                  className="h-3.5 w-3.5 accent-[var(--primary)]"
                />
                <span className="min-w-0 flex-1 truncate">{notebook.name}</span>
                <span className="shrink-0 text-[11px] text-muted">
                  {t("book.recordsCount", { n: String(notebook.record_count ?? 0) })}
                </span>
              </label>
            ))
          )}
        </Section>

        <Section title={t("book.secSessions")} hint={t("book.secHint")}>
          {sessionList.length === 0 ? (
            <p className="text-xs text-muted">{t("book.secEmpty")}</p>
          ) : (
            sessionList.slice(0, 30).map((session) => (
              <label
                key={session.id}
                data-testid="bk-src-session"
                data-id={session.id}
                className="flex cursor-pointer items-center gap-2 text-xs"
              >
                <input
                  type="checkbox"
                  checked={sessionIds.includes(session.id)}
                  disabled={busy}
                  onChange={() => toggle(setSessionIds, session.id)}
                  className="h-3.5 w-3.5 accent-[var(--primary)]"
                />
                <span className="min-w-0 flex-1 truncate">
                  {session.title || t("chat.untitled")}
                </span>
                <span className="shrink-0 text-[11px] text-muted">
                  {formatDateTime(session.updated_at, lang)}
                </span>
              </label>
            ))
          )}
        </Section>

        <Section title={t("book.secQuestions")} hint={t("book.questionFilterHint")}>
          {QUESTION_MODES.map((mode) => (
            <label
              key={mode}
              data-testid="bk-src-question"
              data-mode={mode}
              className="flex cursor-pointer items-center gap-2 text-xs"
            >
              <input
                type="radio"
                name="bk-question-mode"
                checked={questionMode === mode}
                disabled={busy}
                onChange={() => setQuestionMode(mode)}
                className="h-3.5 w-3.5 accent-[var(--primary)]"
              />
              <span className="flex-1">{t(`book.questionMode_${mode}`)}</span>
              {mode !== "none" && counts && (
                <span className="shrink-0 text-[11px] text-muted">
                  {mode === "all"
                    ? t("book.questionCountAll", { n: String(counts.all) })
                    : t("book.questionCountWrong", { n: String(counts.wrong) })}
                </span>
              )}
            </label>
          ))}
        </Section>
      </div>

      {error && <p className="text-xs text-danger">{error}</p>}
      {!hasSources && <p className="text-xs text-muted">{t("book.sourcesRequired")}</p>}

      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="bk-create"
          disabled={!ready}
          onClick={submit}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
        >
          {busy ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <BookPlus className="h-3.5 w-3.5" />
          )}
          {busy ? t("book.creating") : t("book.create")}
        </button>
        <span className="text-xs text-muted">{t("book.createHint")}</span>
      </div>
    </div>
  );
}
