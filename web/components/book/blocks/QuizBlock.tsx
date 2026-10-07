"use client";

/** 测验块：选项多选（后端按 key 集合完全相等判分）→ 提交 → 对错 + 解析。
 *
 * 判分在服务端（零 LLM），这里只做展示；**answer_key 不进前端视图**（parseQuiz
 * 就不取它），答完也只由服务端回对错——选项里看不到答案。
 * 已答状态以页详情带回的最近一次作答为准，本地这次的结果盖在它上面。
 */

import { useState } from "react";
import { Check, Loader2, RotateCcw, X } from "lucide-react";
import { useAttempt } from "@/hooks/useBook";
import { useI18n } from "@/hooks/useI18n";
import { parseQuiz } from "@/lib/book";
import { errorText } from "@/lib/errors";
import type { BookAttempt, BookAttemptResponse, BookBlock } from "@/types/api";

interface Props {
  block: BookBlock;
  bookId: string;
  pageId: string;
  attempt: BookAttempt | null;
}

export default function QuizBlock({ block, bookId, pageId, attempt }: Props) {
  const { t } = useI18n();
  const view = parseQuiz(block.payload);
  const [picked, setPicked] = useState<string[]>(() => attempt?.answer ?? []);
  const [local, setLocal] = useState<BookAttemptResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const submit = useAttempt(bookId, pageId);

  // 服务端那份（刷新/翻页回来）兜底；本地刚提交的结果优先
  const result: { correct: boolean; explanation: string } | null =
    local ?? (attempt ? { correct: attempt.correct, explanation: view.explanation } : null);

  const toggle = (key: string) => {
    if (result) {
      return;
    }
    setPicked((prev) =>
      prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]
    );
  };

  const send = () => {
    if (picked.length === 0) {
      setError(t("book.quizPick"));
      return;
    }
    setError(null);
    submit.mutate(
      { blockId: block.id, answer: picked },
      {
        onSuccess: (data) => setLocal(data),
        onError: (err) => setError(errorText(err, t("common.requestFailed"))),
      }
    );
  };

  return (
    <div className="space-y-3" data-testid="bk-quiz" data-block-id={block.id}>
      <p className="text-sm font-medium">{view.stem}</p>
      <div className="space-y-1.5">
        {view.options.map((option) => {
          const active = picked.includes(option.key);
          return (
            <button
              key={option.key}
              type="button"
              data-testid="bk-quiz-option"
              data-key={option.key}
              data-active={active ? "true" : "false"}
              disabled={Boolean(result) || submit.isPending}
              onClick={() => toggle(option.key)}
              className={`flex w-full items-start gap-2.5 rounded-lg border px-3 py-2 text-left text-sm transition-colors disabled:cursor-default ${
                active ? "border-primary/50 bg-primary/10" : "border-border hover:bg-accent"
              }`}
            >
              <span
                className={`mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px] ${
                  active ? "border-primary text-primary" : "border-border text-muted"
                }`}
              >
                {option.key}
              </span>
              <span className="min-w-0 flex-1">{option.text}</span>
            </button>
          );
        })}
      </div>

      {result && (
        <div
          data-testid="bk-quiz-result"
          data-correct={result.correct ? "true" : "false"}
          className={`rounded-lg border px-3 py-2 text-sm ${
            result.correct ? "border-success/40 text-success" : "border-danger/40 text-danger"
          }`}
        >
          <span className="inline-flex items-center gap-1.5 font-medium">
            {result.correct ? <Check className="h-3.5 w-3.5" /> : <X className="h-3.5 w-3.5" />}
            {result.correct ? t("book.quizCorrect") : t("book.quizWrong")}
          </span>
          {result.explanation && <p className="mt-1 text-xs text-muted">{result.explanation}</p>}
        </div>
      )}

      {error && <p className="text-xs text-danger">{error}</p>}

      <div className="flex items-center gap-2">
        {result ? (
          <button
            type="button"
            onClick={() => {
              setLocal(null);
              setPicked([]);
              setError(null);
            }}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {t("book.quizRetry")}
          </button>
        ) : (
          <button
            type="button"
            data-testid="bk-quiz-submit"
            disabled={submit.isPending}
            onClick={send}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
          >
            {submit.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {t("book.quizSubmit")}
          </button>
        )}
      </div>
    </div>
  );
}
