"use client";

/** 题目卡（§7.4）：题干 + 作答区 + 判分结果 + 掌握度条，可编辑/删除。
 *
 * 作答本地先存草稿，提交后由后端判分（客观题确定性、简答可能走 LLM 判分器），
 * 结果与题目的新状态一起回来——掌握度条当场动，不用刷新。判分不像聊天回合，
 * 不用等 WS 事件，一次请求就完。
 */

import { useState } from "react";
import { Check, Loader2, Pencil, Sparkles, Trash2, X } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import {
  useDeleteQuestion,
  useSubmitAttempt,
  useUpdateQuestion,
  type QuestionInput,
} from "@/hooks/useQuestions";
import type { AttemptResponse, Question } from "@/types/api";
import Markdown from "@/components/chat/Markdown";
import QuestionForm, {
  formValuesOf,
  toInput,
  validate,
  type QuestionFormValues,
} from "./QuestionForm";

/** 选项标签：位置推 A/B/C/D（与后端存法一致，多选用得到）。 */
export function optionLabel(index: number): string {
  return String.fromCharCode(65 + index);
}

const LABEL_KEYS = {
  single: "questions.type_single",
  multi: "questions.type_multi",
  short: "questions.type_short",
} as const;

const DIFFICULTY_KEYS: Record<string, string> = {
  easy: "questions.difficulty_easy",
  medium: "questions.difficulty_medium",
  hard: "questions.difficulty_hard",
};

/** 多选答案存成升序拼接（"AC"）；作答也按同样规则归一，比较才不挑顺序。 */
function normalizeChoice(answer: string): string {
  return [...new Set(answer.toUpperCase().replace(/[^A-Z]/g, ""))].sort().join("");
}

export default function QuestionCard({
  question,
  language,
}: {
  question: Question;
  language: string;
}) {
  const { t } = useI18n();
  const [choice, setChoice] = useState<string>("");
  const [text, setText] = useState("");
  const [result, setResult] = useState<AttemptResponse["grading"] | null>(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<QuestionFormValues>(() => formValuesOf(question));
  const [formError, setFormError] = useState<string | null>(null);
  const submit = useSubmitAttempt();
  const update = useUpdateQuestion();
  const remove = useDeleteQuestion();

  const answer = question.type === "short" ? text.trim() : choice;
  const canSubmit = answer.length > 0 && !submit.isPending;

  const toggleChoice = (label: string) => {
    setResult(null);
    if (question.type === "single") {
      setChoice(label);
      return;
    }
    const picked = new Set(choice.split(""));
    if (picked.has(label)) {
      picked.delete(label);
    } else {
      picked.add(label);
    }
    setChoice([...picked].sort().join(""));
  };

  const send = async () => {
    try {
      const response = await submit.mutateAsync({
        id: question.id,
        answer: question.type === "short" ? answer : normalizeChoice(answer),
        language,
      });
      setResult(response.grading);
    } catch (error) {
      setResult(null);
      setFormError(String(error));
    }
  };

  const saveEdit = async () => {
    const problem = validate(form);
    if (problem) {
      setFormError(t(problem));
      return;
    }
    setFormError(null);
    try {
      await update.mutateAsync({ id: question.id, ...(toInput(form) as QuestionInput) });
      setEditing(false);
    } catch (error) {
      setFormError(String(error));
    }
  };

  const handleDelete = () => {
    if (window.confirm(t("questions.deleteConfirm"))) {
      remove.mutate(question.id);
    }
  };

  if (editing) {
    return (
      <li
        data-testid="q-card"
        data-id={question.id}
        className="rounded-xl border border-border bg-surface p-4"
      >
        <QuestionForm
          values={form}
          onChange={setForm}
          onSubmit={() => void saveEdit()}
          onCancel={() => {
            setForm(formValuesOf(question));
            setFormError(null);
            setEditing(false);
          }}
          pending={update.isPending}
          testidPrefix="q-edit-form"
        />
        {formError && <p className="mt-2 text-xs text-danger">{formError}</p>}
      </li>
    );
  }

  const picked = new Set(question.type === "short" ? [] : choice.split(""));

  return (
    <li
      data-testid="q-card"
      data-id={question.id}
      data-type={question.type}
      className="rounded-xl border border-border bg-surface p-4"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="markdown-body text-sm">
            <Markdown text={question.stem} />
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            data-testid="q-edit"
            onClick={() => {
              setForm(formValuesOf(question));
              setFormError(null);
              setEditing(true);
            }}
            title={t("questions.edit")}
            className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <Pencil className="h-4 w-4" />
          </button>
          <button
            type="button"
            data-testid="q-delete"
            onClick={handleDelete}
            title={t("questions.delete")}
            className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
        <span className="rounded bg-accent/60 px-1.5 py-0.5">{t(LABEL_KEYS[question.type])}</span>
        {question.difficulty && (
          <span className="rounded bg-accent/60 px-1.5 py-0.5">
            {t(DIFFICULTY_KEYS[question.difficulty] ?? "questions.difficulty_medium")}
          </span>
        )}
        {question.knowledge_point && <span>{question.knowledge_point}</span>}
        {question.wrong_count > 0 && (
          <span className="text-danger">
            {t("questions.wrongCount", { n: String(question.wrong_count) })}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1.5">
          <span>{t("questions.mastery")}</span>
          <span className="inline-block h-1.5 w-16 overflow-hidden rounded-full bg-accent">
            <span
              data-testid="q-mastery"
              data-value={question.mastery.toFixed(2)}
              className="block h-full rounded-full bg-primary"
              style={{ width: `${Math.round(question.mastery * 100)}%` }}
            />
          </span>
          <span>{Math.round(question.mastery * 100)}%</span>
        </span>
      </div>

      <div className="mt-3 space-y-1.5" data-testid="q-answer">
        {question.type === "short" ? (
          <textarea
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setResult(null);
            }}
            placeholder={t("questions.answerHintShort")}
            rows={2}
            className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
        ) : (
          question.options.map((option, index) => {
            const label = optionLabel(index);
            const selected = picked.has(label);
            return (
              <button
                key={label}
                type="button"
                data-testid="q-option"
                data-label={label}
                data-selected={selected}
                onClick={() => toggleChoice(label)}
                className={`flex w-full items-start gap-2 rounded-lg border px-2.5 py-1.5 text-left text-sm transition-colors ${
                  selected
                    ? "border-primary/60 bg-primary/5"
                    : "border-border hover:border-primary/40 hover:bg-accent/40"
                }`}
              >
                <span
                  className={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center text-[11px] ${
                    question.type === "multi"
                      ? "rounded-[3px] border border-current"
                      : "rounded-full border border-current"
                  }`}
                >
                  {selected && <Check className="h-3 w-3" />}
                </span>
                <span className="min-w-0 flex-1 markdown-body">
                  <Markdown text={option} />
                </span>
              </button>
            );
          })
        )}
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="q-submit"
          disabled={!canSubmit}
          onClick={() => void send()}
          title={canSubmit ? undefined : t("questions.answerHint")}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
        >
          {submit.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {t("questions.submit")}
        </button>
        {result && (
          <span
            data-testid="q-result"
            data-correct={result.correct}
            className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ${
              result.correct ? "bg-primary/10 text-primary" : "bg-danger/10 text-danger"
            }`}
          >
            {result.correct ? <Check className="h-3 w-3" /> : <X className="h-3 w-3" />}
            {result.correct ? t("questions.correct") : t("questions.wrong")}
            <span>· {t("questions.scoreLine", { score: result.score.toFixed(2) })}</span>
            {result.source === "llm" && (
              <span className="inline-flex items-center gap-0.5">
                <Sparkles className="h-3 w-3" />
                {t("questions.gradedByLlm")}
              </span>
            )}
          </span>
        )}
      </div>

      {result && (
        <div className="mt-2 space-y-1.5 rounded-lg bg-accent/40 px-3 py-2 text-xs">
          <p className="whitespace-pre-wrap">{result.feedback}</p>
          <div data-testid="q-explanation" className="text-muted">
            <span className="font-medium">{t("questions.reference")}：</span>
            <span className="markdown-body inline-block align-top">
              <Markdown text={question.answer} />
            </span>
            {question.explanation && (
              <>
                <span className="ml-3 font-medium">{t("questions.explanation")}：</span>
                <span className="markdown-body inline-block align-top">
                  <Markdown text={question.explanation} />
                </span>
              </>
            )}
          </div>
        </div>
      )}
      {!result && formError && <p className="mt-2 text-xs text-danger">{formError}</p>}
    </li>
  );
}
