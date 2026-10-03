"use client";

/** 举一反三面板（§7.4 题库增强）：相似题列表 + 变式题生成预览。
 *
 * 三级递进：题库相似题（零 LLM，打开面板即拉）→ 知识库素材依据出题
 * （mode=auto，后端检索 KB）→ 纯 AI（mode=ai）。生成结果不落库，
 * 勾选「采纳」才批量入库（source=variant，parent_id 指向来源题）。
 */

import { useState } from "react";
import { Check, Loader2, Sparkles, Wand2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useAdoptVariants, useGenerateVariants, useSimilarQuestions } from "@/hooks/useQuestions";
import { errorText } from "@/lib/errors";
import { draftToAdoptInput, optionLabel, similarityPercent } from "@/lib/questions";
import type { Question, VariantsResponse } from "@/types/api";
import Markdown from "@/components/chat/Markdown";

export default function QuestionVariantsPanel({
  question,
  language,
}: {
  question: Question;
  language: string;
}) {
  const { t } = useI18n();
  const similar = useSimilarQuestions(question.id, true);
  const generate = useGenerateVariants();
  const adopt = useAdoptVariants();
  const [mode, setMode] = useState<"auto" | "ai">("auto");
  const [count, setCount] = useState(3);
  const [result, setResult] = useState<VariantsResponse | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [adopted, setAdopted] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const runGenerate = async () => {
    setError(null);
    setAdopted(0);
    try {
      const data = await generate.mutateAsync({ id: question.id, mode, count, language });
      setResult(data);
      // 默认全勾——不想要的手动去勾；采纳前不入库，胆子可以大一点。
      setChecked(new Set(data.variants.map((_, index) => index)));
    } catch (cause) {
      setResult(null);
      setChecked(new Set());
      setError(errorText(cause, t("common.requestFailed")));
    }
  };

  const toggle = (index: number) => {
    setChecked((previous) => {
      const next = new Set(previous);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  };

  const adoptSelected = async () => {
    if (!result || checked.size === 0) {
      return;
    }
    setError(null);
    try {
      const picks = result.variants.filter((_, index) => checked.has(index)).map(draftToAdoptInput);
      const response = await adopt.mutateAsync({ parentId: question.id, questions: picks });
      setAdopted(response.questions.length);
      setResult(null);
      setChecked(new Set());
    } catch (cause) {
      setError(errorText(cause, t("common.requestFailed")));
    }
  };

  return (
    <section
      data-testid="q-variants-panel"
      className="mt-3 space-y-3 rounded-xl border border-border bg-accent/20 p-3"
    >
      {/* 第一级：题库里已有的相似题（词法相似度，零 LLM） */}
      <div>
        <h4 className="text-xs font-medium">{t("questions.similarTitle")}</h4>
        {similar.isLoading ? (
          <p className="mt-1 text-xs text-muted">{t("questions.loading")}</p>
        ) : similar.data && similar.data.items.length > 0 ? (
          <ul className="mt-1.5 space-y-1">
            {similar.data.items.map((item) => (
              <li
                key={item.question.id}
                data-testid="q-similar-item"
                data-id={item.question.id}
                className="flex items-start gap-2 text-xs"
              >
                <span className="min-w-0 flex-1">
                  <Markdown text={item.question.stem} />
                </span>
                <span data-testid="q-similar-score" className="shrink-0 text-muted">
                  {t("questions.similarScore", {
                    percent: String(similarityPercent(item.score)),
                  })}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-xs text-muted">{t("questions.similarEmpty")}</p>
        )}
        {similar.data?.low_confidence && (
          <p data-testid="q-similar-low" className="mt-1 text-[11px] text-muted">
            {t("questions.similarLowConfidence")}
          </p>
        )}
      </div>

      {/* 第二/三级：生成变式题 */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          data-testid="q-variants-mode"
          value={mode}
          onChange={(event) => setMode(event.target.value as "auto" | "ai")}
          className="rounded-lg border border-border bg-transparent px-2 py-1 text-xs outline-none focus:border-primary/50"
        >
          <option value="auto">{t("questions.variantsModeAuto")}</option>
          <option value="ai">{t("questions.variantsModeAi")}</option>
        </select>
        <select
          data-testid="q-variants-count"
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
          className="rounded-lg border border-border bg-transparent px-2 py-1 text-xs outline-none focus:border-primary/50"
        >
          {[3, 5].map((value) => (
            <option key={value} value={value}>
              {t("questions.variantsCount", { n: String(value) })}
            </option>
          ))}
        </select>
        <button
          type="button"
          data-testid="q-variants-generate"
          disabled={generate.isPending}
          onClick={() => void runGenerate()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
        >
          {generate.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Wand2 className="h-3.5 w-3.5" />
          )}
          {generate.isPending ? t("questions.variantsPending") : t("questions.variantsGenerate")}
        </button>
      </div>

      {result && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span
              data-testid="q-variants-origin"
              data-origin={result.origin}
              className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-primary"
            >
              <Sparkles className="h-3 w-3" />
              {result.origin === "kb"
                ? t("questions.variantsOriginKb")
                : t("questions.variantsOriginAi")}
            </span>
            <span className="text-muted">{t("questions.variantsPreviewHint")}</span>
          </div>
          {result.degraded && (
            <p data-testid="q-variants-degraded" className="text-[11px] text-muted">
              {t("questions.variantsDegraded")}
            </p>
          )}
          {result.sources.length > 0 && (
            <div
              data-testid="q-variants-sources"
              className="flex flex-wrap gap-x-2 text-[11px] text-muted"
            >
              <span className="font-medium">{t("questions.variantsSources")}：</span>
              {result.sources.map((source) => (
                <span key={`${source.kb_id}-${source.doc_id}-${source.page ?? 0}`}>
                  {source.kb_name}
                  {source.filename ? ` · ${source.filename}` : ""}
                  {source.page != null ? ` · p.${source.page}` : ""}
                </span>
              ))}
            </div>
          )}

          <ul className="space-y-2">
            {result.variants.map((draft, index) => (
              <li
                key={index}
                data-testid="q-draft"
                className={`rounded-lg border p-2.5 text-sm ${
                  checked.has(index) ? "border-primary/50 bg-primary/5" : "border-border"
                }`}
              >
                <div className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    data-testid="q-draft-check"
                    data-check={checked.has(index)}
                    checked={checked.has(index)}
                    onChange={() => toggle(index)}
                    className="mt-1 accent-[var(--primary)]"
                  />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <div className="markdown-body">
                      <Markdown text={draft.stem} />
                    </div>
                    {draft.options.length > 0 && (
                      <ul className="space-y-0.5 text-xs">
                        {draft.options.map((option, optionIndex) => (
                          <li key={optionIndex} className="markdown-body">
                            <span className="text-muted">{optionLabel(optionIndex)}. </span>
                            <Markdown text={option} />
                          </li>
                        ))}
                      </ul>
                    )}
                    {/* Markdown 渲染成 div，容器不能用 p（嵌套告警） */}
                    <div className="text-xs text-muted">
                      <span className="font-medium">{t("questions.reference")}：</span>
                      <span className="markdown-body inline-block align-top">
                        <Markdown text={draft.answer} />
                      </span>
                    </div>
                    {draft.explanation && (
                      <div className="text-xs text-muted">
                        <span className="font-medium">{t("questions.explanation")}：</span>
                        <span className="markdown-body inline-block align-top">
                          <Markdown text={draft.explanation} />
                        </span>
                      </div>
                    )}
                    {draft.duplicate_score >= 0.8 && (
                      <p data-testid="q-draft-dup" className="text-[11px] text-danger">
                        {t("questions.variantsDup", {
                          percent: String(similarityPercent(draft.duplicate_score)),
                        })}
                      </p>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ul>

          <button
            type="button"
            data-testid="q-variants-adopt"
            disabled={checked.size === 0 || adopt.isPending}
            onClick={() => void adoptSelected()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
          >
            {adopt.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
            {t("questions.variantsAdopt", { n: String(checked.size) })}
          </button>
        </div>
      )}

      {adopted > 0 && (
        <p data-testid="q-variants-done" className="text-xs text-primary">
          {t("questions.variantsAdoptDone", { n: String(adopted) })}
        </p>
      )}
      {error && <p className="text-xs text-danger">{error}</p>}
    </section>
  );
}
