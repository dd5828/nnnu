"use client";

/** 答题结果卡（§7.5）：聊天里那次 `mastery` 工具调用的判分结果，摊开给人看。
 *
 * 答题通路搬进聊天之后，工具输出那句纯文本（「第 1/2 题 ✓ …」）对人太糊：题干、
 * 你选了什么、对错、解析、这一下掌握度走到哪儿，都要一眼看到。所以 `mastery` 的
 * 判分动作（quiz / probe / assess / grade）不再走通用 `ToolCallCard` 的折叠 JSON，
 * 改由这里渲染；其它动作（status/build/paths/switch/leave）照旧走通用卡。
 *
 * **答案键是工具输出里带的，不是前端算的**：`question.answer` 只在本题判分之后
 * 才由服务端塞进 detail（见 `tools/builtin/mastery_tool.py` 模块头 ④），前端只管画。
 * testid 一律 `m-*` 前缀，跟题库页的 `q-*` 分开——两处选择器长得像会互相撞。
 */

import { Check, Sparkles, X } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { UiToolCall } from "@/hooks/useChat";
import { masteryPercent } from "@/lib/learning";
import Markdown from "./Markdown";

/** 会摊成结果卡的动作（其余动作没有卡片可讲）。 */
const CARD_ACTIONS = new Set(["quiz", "probe", "assess", "grade"]);

interface CardQuestion {
  id?: string;
  stem?: string;
  type?: string;
  options?: string[];
  answer?: string;
  explanation?: string;
}

interface CardGrading {
  correct?: boolean;
  score?: number;
  feedback?: string;
  source?: string;
}

interface ResultCard {
  question?: CardQuestion;
  answer?: string;
  answered?: boolean;
  note?: string;
  grading?: CardGrading;
  mastery?: number;
  cleared?: boolean;
}

interface ResultDetail {
  action?: string;
  cards?: ResultCard[];
  mastery?: number;
  gate?: number | null;
  gate_kind?: string;
  cleared?: boolean;
  replayed?: boolean;
}

/** 这个调用该不该由结果卡接管：判分动作 + 已成功 + 至少有一张卡。 */
export function isGradedMasteryCall(call: UiToolCall): boolean {
  if (call.name !== "mastery" || call.ok !== true || !call.detail) {
    return false;
  }
  const detail = call.detail as ResultDetail;
  if (!CARD_ACTIONS.has(String(detail.action ?? ""))) {
    return false;
  }
  return Array.isArray(detail.cards) && detail.cards.length > 0;
}

function optionLabel(index: number): string {
  return String.fromCharCode(65 + index);
}

function GradingChip({ card, assess }: { card: ResultCard; assess: boolean }) {
  const { t } = useI18n();
  const grading = card.grading;
  if (!grading) {
    return (
      <span data-testid="m-quiz-result" data-correct="" className="text-[11px] text-muted">
        {t(card.answered === false ? "chat.quizNotAnswered" : "chat.quizSkipped")}
      </span>
    );
  }
  const correct = Boolean(grading.correct);
  const label = assess
    ? correct
      ? t("chat.assessPassed")
      : t("chat.assessFailed")
    : correct
      ? t("questions.correct")
      : t("questions.wrong");
  return (
    <span
      data-testid="m-quiz-result"
      data-correct={correct ? "true" : "false"}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ${
        correct ? "bg-primary/10 text-primary" : "bg-danger/10 text-danger"
      }`}
    >
      {correct ? <Check className="h-3 w-3" /> : <X className="h-3 w-3" />}
      {label}
      {!assess && grading.score !== undefined && (
        <span>· {t("questions.scoreLine", { score: grading.score.toFixed(2) })}</span>
      )}
      {grading.source === "llm" || grading.source === "assessor" ? (
        <span className="inline-flex items-center gap-0.5">
          <Sparkles className="h-3 w-3" />
          {t("questions.gradedByLlm")}
        </span>
      ) : null}
    </span>
  );
}

function CardBlock({
  card,
  assess,
  mastery,
  gate,
  gateKind,
  cleared,
}: {
  card: ResultCard;
  assess: boolean;
  mastery: number | undefined;
  gate: number | null | undefined;
  gateKind: string | undefined;
  cleared: boolean | undefined;
}) {
  const { t } = useI18n();
  const question = card.question ?? {};
  const options = question.options ?? [];
  // 掌握度不是每张卡都有（grade 的回放卡就只有判分结果）：没有就不画那一行，
  // 否则会读成「掌握度 0」，看着像掉分。
  const shownMastery = mastery ?? card.mastery;
  const shownCleared = cleared ?? card.cleared ?? false;
  return (
    <div className="flex flex-col gap-2 border-t border-border/40 px-3 py-2 first:border-t-0">
      {question.stem && (
        <div className="markdown-body text-sm" data-testid="m-quiz-stem">
          <Markdown text={question.stem} />
        </div>
      )}

      {/* 选项：是对的那个标绿，用户选错的那个标红，其余保持素色 */}
      {options.length > 0 && (
        <ul className="space-y-1" data-testid="m-quiz-options">
          {options.map((option, index) => {
            const label = optionLabel(index);
            const chosen = (card.answer ?? "").toUpperCase().includes(label);
            const isKey = (question.answer ?? "").toUpperCase().includes(label);
            return (
              <li
                key={label}
                data-testid="m-quiz-option"
                data-label={label}
                data-chosen={chosen ? "true" : "false"}
                data-key={isKey ? "true" : "false"}
                className={`flex items-start gap-2 rounded-lg border px-2.5 py-1.5 text-sm ${
                  isKey
                    ? "border-success/50 bg-success/5"
                    : chosen
                      ? "border-danger/50 bg-danger/5"
                      : "border-border"
                }`}
              >
                <span className="mt-0.5 shrink-0 font-mono text-[11px] text-muted">{label}</span>
                <span className="min-w-0 flex-1 markdown-body">
                  <Markdown text={option} />
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {/* 简答/评定的作答原文 */}
      {options.length === 0 && card.answer && (
        <p className="rounded-lg bg-accent/40 px-2.5 py-1.5 text-xs" data-testid="m-quiz-answer">
          <span className="mr-1 text-muted">{t("chat.quizYourAnswer")}</span>
          <span className="whitespace-pre-wrap">{card.answer}</span>
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <GradingChip card={card} assess={assess} />
        {shownMastery !== undefined && (
          <span className="inline-flex items-center gap-1.5 text-[11px] text-muted">
            <span>{t("learning.masteryLine", { n: String(masteryPercent(shownMastery)) })}</span>
            <span className="inline-block h-1.5 w-16 overflow-hidden rounded-full bg-accent">
              <span
                data-testid="m-quiz-mastery"
                data-value={masteryPercent(shownMastery)}
                data-cleared={shownCleared ? "true" : "false"}
                className={`block h-full rounded-full ${
                  shownCleared ? "bg-success" : "bg-primary"
                }`}
                style={{ width: `${masteryPercent(shownMastery)}%` }}
              />
            </span>
            <span data-gate-kind={gateKind ?? ""} data-gate={gate ?? ""}>
              {gateKind === "quantitative" && gate !== null && gate !== undefined
                ? t("learning.gateQuantitative", { gate: String(Math.round(gate)) })
                : t("learning.gateQualitative")}
            </span>
          </span>
        )}
      </div>

      {card.grading?.feedback && (
        <p
          className="whitespace-pre-wrap rounded-lg bg-accent/40 px-2.5 py-1.5 text-xs"
          data-testid="m-quiz-feedback"
        >
          {card.grading.feedback}
        </p>
      )}

      {/* 参考答案只在判分之后才有（服务端给的），没有就不渲染 */}
      {(question.answer || question.explanation) && (
        <div className="text-xs text-muted" data-testid="m-quiz-reference">
          {question.answer && (
            <>
              <span className="font-medium">{t("questions.reference")}：</span>
              <span className="markdown-body inline-block align-top">
                <Markdown text={question.answer} />
              </span>
            </>
          )}
          {question.explanation && (
            <>
              <span className="ml-3 font-medium">{t("questions.explanation")}：</span>
              <span className="markdown-body inline-block align-top">
                <Markdown text={question.explanation} />
              </span>
            </>
          )}
        </div>
      )}

      {card.note && <p className="text-[11px] text-muted">{card.note}</p>}
    </div>
  );
}

export default function MasteryResultCard({ call }: { call: UiToolCall }) {
  const { t } = useI18n();
  const detail = (call.detail ?? {}) as ResultDetail;
  const cards = detail.cards ?? [];
  const assess = detail.action === "assess";
  return (
    <div
      data-testid="m-card"
      data-action={detail.action ?? ""}
      className="mb-2 w-full overflow-hidden rounded-lg border border-border/70 bg-surface text-sm"
    >
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="text-xs font-medium text-muted">
          {t(assess ? "chat.assessResult" : "chat.quizResult")}
        </span>
        {detail.replayed && <span className="text-[11px] text-muted">{t("chat.quizReplay")}</span>}
      </div>
      {cards.map((card, index) => (
        <CardBlock
          key={card.question?.id || `card-${index}`}
          card={card}
          assess={assess}
          mastery={detail.mastery}
          gate={detail.gate}
          gateKind={detail.gate_kind}
          cleared={detail.cleared}
        />
      ))}
    </div>
  );
}
