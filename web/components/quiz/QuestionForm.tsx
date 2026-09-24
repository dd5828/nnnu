"use client";

/** 题目表单（§7.4）：题库页新建与卡片内编辑共用一份，字段跟 §8.2 的列对齐。
 *
 * 选项是「一行一条」的裸文本（标签 A/B/C/D 由位置推，与后端存法一致）；答案对客观题
 * 写标签（多选写 ABD），简答写参考答案。校验只做界面能说的那几条，结构合法性以后端为准。
 */

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { QuestionInput } from "@/hooks/useQuestions";
import type { Question, QuestionType } from "@/types/api";

export const QUESTION_TYPES: QuestionType[] = ["single", "multi", "short"];
export const DIFFICULTIES = ["easy", "medium", "hard"];

export interface QuestionFormValues {
  stem: string;
  type: QuestionType;
  options: string;
  answer: string;
  explanation: string;
  knowledgePoint: string;
  difficulty: string;
}

export function emptyFormValues(): QuestionFormValues {
  return {
    stem: "",
    type: "single",
    options: "",
    answer: "",
    explanation: "",
    knowledgePoint: "",
    difficulty: "medium",
  };
}

export function formValuesOf(question: Question): QuestionFormValues {
  return {
    stem: question.stem,
    type: question.type,
    options: question.options.join("\n"),
    answer: question.answer,
    explanation: question.explanation ?? "",
    knowledgePoint: question.knowledge_point,
    difficulty: question.difficulty,
  };
}

const inputClass =
  "w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50";

/** 表单值 → 提交体：选项按行切、简答不带选项（客观题选项至少两条由调用方校验）。 */
export function toInput(values: QuestionFormValues): QuestionInput {
  const options =
    values.type === "short"
      ? []
      : values.options
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
  return {
    stem: values.stem.trim(),
    type: values.type,
    options,
    answer: values.answer.trim(),
    explanation: values.explanation.trim() || null,
    knowledge_point: values.knowledgePoint.trim(),
    difficulty: values.difficulty,
  };
}

/** 校验：返回文案键，通过则返回 null（前端只说能即时判断的问题）。 */
export function validate(values: QuestionFormValues): string | null {
  if (!values.stem.trim()) {
    return "questions.stemRequired";
  }
  if (!values.answer.trim()) {
    return "questions.answerRequired";
  }
  if (values.type !== "short") {
    const options = values.options
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (options.length < 2) {
      return "questions.optionsRequired";
    }
  }
  return null;
}

export default function QuestionForm({
  values,
  onChange,
  onSubmit,
  onCancel,
  pending,
  testidPrefix,
}: {
  values: QuestionFormValues;
  onChange: (next: QuestionFormValues) => void;
  onSubmit: () => void;
  onCancel: () => void;
  pending: boolean;
  testidPrefix: string;
}) {
  const { t } = useI18n();
  const [error, setError] = useState<string | null>(null);
  const patch = (part: Partial<QuestionFormValues>) => onChange({ ...values, ...part });

  const submit = () => {
    const problem = validate(values);
    if (problem) {
      setError(t(problem));
      return;
    }
    setError(null);
    onSubmit();
  };

  return (
    <div className="space-y-2 rounded-xl border border-border bg-surface p-4">
      <textarea
        data-testid={`${testidPrefix}-stem`}
        value={values.stem}
        onChange={(event) => patch({ stem: event.target.value })}
        placeholder={t("questions.stemPlaceholder")}
        rows={2}
        className={inputClass}
      />
      <div className="flex flex-wrap gap-2">
        <select
          data-testid={`${testidPrefix}-type`}
          value={values.type}
          onChange={(event) => patch({ type: event.target.value as QuestionType })}
          className={inputClass}
        >
          {QUESTION_TYPES.map((value) => (
            <option key={value} value={value}>
              {t(`questions.type_${value}`)}
            </option>
          ))}
        </select>
        <select
          data-testid={`${testidPrefix}-difficulty`}
          value={values.difficulty}
          onChange={(event) => patch({ difficulty: event.target.value })}
          className={inputClass}
        >
          {DIFFICULTIES.map((value) => (
            <option key={value} value={value}>
              {t(`questions.difficulty_${value}`)}
            </option>
          ))}
        </select>
        <input
          data-testid={`${testidPrefix}-knowledge-point`}
          value={values.knowledgePoint}
          onChange={(event) => patch({ knowledgePoint: event.target.value })}
          placeholder={t("questions.knowledgePointPlaceholder")}
          className={`${inputClass} flex-1`}
        />
      </div>
      {values.type !== "short" && (
        <textarea
          data-testid={`${testidPrefix}-options`}
          value={values.options}
          onChange={(event) => patch({ options: event.target.value })}
          placeholder={t("questions.optionsPlaceholder")}
          rows={2}
          className={inputClass}
        />
      )}
      <textarea
        data-testid={`${testidPrefix}-answer`}
        value={values.answer}
        onChange={(event) => patch({ answer: event.target.value })}
        placeholder={
          values.type === "short" ? t("questions.keyPlaceholder") : t("questions.answerPlaceholder")
        }
        rows={values.type === "short" ? 2 : 1}
        className={inputClass}
      />
      <textarea
        data-testid={`${testidPrefix}-explanation`}
        value={values.explanation}
        onChange={(event) => patch({ explanation: event.target.value })}
        placeholder={t("questions.explanationPlaceholder")}
        rows={2}
        className={inputClass}
      />
      {error && <p className="text-xs text-danger">{error}</p>}
      <div className="flex gap-2">
        <button
          type="button"
          data-testid={`${testidPrefix}-save`}
          onClick={submit}
          disabled={pending}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
        >
          {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {t("common.save")}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg px-3.5 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
        >
          {t("common.close")}
        </button>
      </div>
    </div>
  );
}
