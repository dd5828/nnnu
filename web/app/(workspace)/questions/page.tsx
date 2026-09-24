"use client";

/**
 * 题库页（§7.4 / §9.1）：筛选条 + 题目卡列表 + 手动新增。
 *
 * 判分在卡片里当场做（后端 `POST /questions/{id}/attempt`）；做题产生的错题就是
 * 「错题」这个筛选视图（wrong_count > 0），不需要额外的「一键入库」动作。
 */

import { useEffect, useMemo, useState } from "react";
import { ListChecks, Loader2, Plus, Search, X } from "lucide-react";
import QuestionCard from "@/components/quiz/QuestionCard";
import QuestionForm, { emptyFormValues, toInput, validate } from "@/components/quiz/QuestionForm";
import { useCreateQuestion, useQuestionList } from "@/hooks/useQuestions";
import { useI18n } from "@/hooks/useI18n";
import type { QuestionFilter } from "@/types/api";

const FILTERS: { value: QuestionFilter; key: string }[] = [
  { value: "all", key: "questions.filterAll" },
  { value: "wrong", key: "questions.filterWrong" },
  { value: "unanswered", key: "questions.filterUnanswered" },
];

export default function QuestionsPage() {
  const { t, lang } = useI18n();
  const [filter, setFilter] = useState<QuestionFilter>("all");
  const [knowledgePoint, setKnowledgePoint] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState(emptyFormValues);
  const [formError, setFormError] = useState<string | null>(null);
  const create = useCreateQuestion();

  // 搜索防抖：边打字边打后端没意义，停手了再查
  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchDraft.trim()), 400);
    return () => clearTimeout(timer);
  }, [searchDraft]);

  const { data, isLoading, error } = useQuestionList({ filter, knowledgePoint, search });
  // 知识点下拉的候选：另外拉一份「全部」的第一页（同一条查询缓存住，切筛选不会重复请求）
  const { data: all } = useQuestionList({ filter: "all" });
  const knowledgePoints = useMemo(
    () =>
      [
        ...new Set((all?.questions ?? []).map((item) => item.knowledge_point).filter(Boolean)),
      ].sort(),
    [all]
  );

  const questions = data?.questions ?? [];
  const counts = data?.counts ?? { all: 0, wrong: 0, unanswered: 0 };

  const submitNew = async () => {
    const problem = validate(form);
    if (problem) {
      setFormError(t(problem));
      return;
    }
    setFormError(null);
    try {
      await create.mutateAsync(toInput(form));
      setForm(emptyFormValues());
      setFormOpen(false);
    } catch (err) {
      setFormError(String(err));
    }
  };

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div>
            <h1 className="text-xl font-semibold">{t("questions.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("questions.desc")}</p>
          </div>
          {!formOpen && (
            <button
              type="button"
              data-testid="q-new"
              onClick={() => setFormOpen(true)}
              className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("questions.newQuestion")}
            </button>
          )}
        </div>

        {formOpen && (
          <div>
            <QuestionForm
              values={form}
              onChange={setForm}
              onSubmit={() => void submitNew()}
              onCancel={() => {
                setForm(emptyFormValues());
                setFormError(null);
                setFormOpen(false);
              }}
              pending={create.isPending}
              testidPrefix="q-new-form"
            />
            {formError && <p className="mt-2 text-xs text-danger">{formError}</p>}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              data-testid={`q-filter-${item.value}`}
              data-active={filter === item.value}
              onClick={() => setFilter(item.value)}
              className={`rounded-full px-3 py-1 text-xs transition-colors ${
                filter === item.value
                  ? "bg-primary text-primary-foreground"
                  : "bg-accent/50 text-muted hover:bg-accent"
              }`}
            >
              {t(item.key)} · {counts[item.value]}
            </button>
          ))}
          {knowledgePoints.length > 0 && (
            <select
              data-testid="q-filter-knowledge"
              value={knowledgePoint}
              onChange={(event) => setKnowledgePoint(event.target.value)}
              className="rounded-lg border border-border bg-transparent px-2 py-1 text-xs outline-none focus:border-primary/50"
            >
              <option value="">{t("questions.knowledgeAll")}</option>
              {knowledgePoints.map((point) => (
                <option key={point} value={point}>
                  {point}
                </option>
              ))}
            </select>
          )}
          <div className="relative ml-auto min-w-40 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input
              data-testid="q-filter-search"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              placeholder={t("questions.searchPlaceholder")}
              className="w-full rounded-lg border border-border bg-transparent py-1 pl-8 pr-7 text-xs outline-none focus:border-primary/50"
            />
            {searchDraft && (
              <button
                type="button"
                onClick={() => setSearchDraft("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted hover:bg-accent hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{String(error)}</p>
        ) : questions.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
            <ListChecks className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("questions.empty")}</p>
            <p className="max-w-xs text-xs text-muted">{t("questions.emptyHint")}</p>
          </div>
        ) : (
          <ul className="space-y-2" data-testid="q-list">
            {questions.map((question) => (
              <QuestionCard key={question.id} question={question} language={lang} />
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
