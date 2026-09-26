"use client";

/** 进行中回合（§6.1 事件流实时渲染）：阶段条 + 思考 + 正文 + 工具 + 引用 + ask_user + 成本。 */

import { useState } from "react";
import { CircleStop, Loader2 } from "lucide-react";
import { useChatStore, type ActiveTurn, type AskUserPrompt } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import CitationsPanel from "./CitationsPanel";
import CostBadge from "./CostBadge";
import Markdown, { useDebouncedText } from "./Markdown";
import MasteryResultCard, { isGradedMasteryCall } from "./MasteryResultCard";
import StageBar from "./StageBar";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";

/** 回合中途的提问卡（§7.2）：题干 + 可点选项 +（可选）自由文本。
 *
 * 选项/题干都由服务端渲染好（学习路径的卡片就是题库里的题），这里只负责显示与回传：
 * 点选项发的是 `label`（判分靠它归一成选项原文），自由文本原样发。 */
function AskUserCard({
  prompt,
  onReply,
}: {
  prompt: AskUserPrompt;
  onReply: (answer: string) => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const canSubmitDraft = draft.trim().length > 0;

  return (
    <div className="w-full rounded-xl border border-primary/30 bg-surface p-3">
      {prompt.context && (
        <div data-testid="ask-context" className="mb-1 text-[11px] text-muted">
          {prompt.context}
        </div>
      )}
      <div className="markdown-body mb-2 text-sm font-medium">
        <Markdown text={prompt.question} />
      </div>
      {prompt.options.length > 0 && (
        <div className="space-y-1.5">
          {prompt.options.map((option) => (
            <button
              key={option.label}
              type="button"
              data-testid="ask-option"
              data-label={option.label}
              onClick={() => onReply(option.label)}
              className="flex w-full items-start gap-2 rounded-lg border border-border px-2.5 py-1.5 text-left text-sm transition-colors hover:border-primary/40 hover:bg-accent/40"
            >
              <span className="min-w-0 flex-1">
                <span className="font-medium">{option.label}</span>
                {option.description && (
                  <span className="markdown-body mt-0.5 block text-xs text-muted">
                    <Markdown text={option.description} />
                  </span>
                )}
              </span>
            </button>
          ))}
        </div>
      )}
      {prompt.allowFreeText && (
        <div className="mt-2 space-y-1.5">
          <textarea
            data-testid="ask-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("chat.askFreeTextHint")}
            rows={3}
            className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
          <button
            type="button"
            data-testid="ask-submit"
            disabled={!canSubmitDraft}
            onClick={() => onReply(draft.trim())}
            className="rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
          >
            {t("chat.askSubmit")}
          </button>
        </div>
      )}
    </div>
  );
}

export default function ActiveTurnView({ turn }: { turn: ActiveTurn }) {
  const { t } = useI18n();
  const stop = useChatStore((s) => s.stop);
  const replyAskUser = useChatStore((s) => s.replyAskUser);
  const capability = useChatStore((s) => s.capability);
  const renderedContent = useDebouncedText(turn.content);
  const running = !turn.terminal;

  return (
    <div className="flex flex-col items-start gap-1.5">
      {/* 阶段条只在跑着的时候显示：终局（error/stopped）后还转圈就是骗人 */}
      {running && <StageBar capability={capability} stages={turn.stages} />}
      {turn.thinking && <ThinkingBlock text={turn.thinking} streaming={running} />}
      {renderedContent && (
        <div className="max-w-full">
          <Markdown text={renderedContent} />
        </div>
      )}
      {!renderedContent && !turn.thinking && running && (
        <div className="flex items-center gap-2 py-1 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("chat.working")}
        </div>
      )}
      {Object.values(turn.toolCalls).map((call) =>
        // 判过分的答题调用摊成结果卡（与历史消息同一条规则）；其余走通用折叠卡
        isGradedMasteryCall(call) ? (
          <MasteryResultCard key={call.call_id} call={call} />
        ) : (
          <ToolCallCard key={call.call_id} call={call} />
        )
      )}
      {turn.citations.length > 0 && <CitationsPanel sources={turn.citations} />}
      {turn.warning && (
        <div className="w-full rounded-lg border border-border/70 bg-accent/40 px-3 py-2 text-xs text-muted">
          {turn.warning}
        </div>
      )}
      {turn.askUser && (
        // key=ask_id：换一张卡就重挂载，草稿文本不会串到下一题
        <AskUserCard key={turn.askUser.ask_id} prompt={turn.askUser} onReply={replyAskUser} />
      )}
      <div className="flex items-center gap-2">
        {turn.cost && <CostBadge tokens={turn.cost.tokens} cost={turn.cost.cost} />}
        {running && (
          <button
            type="button"
            onClick={stop}
            className="inline-flex items-center gap-1 rounded-full bg-danger/10 px-2.5 py-1 text-[11px] text-danger transition-colors hover:bg-danger/20"
          >
            <CircleStop className="h-3 w-3" />
            {t("chat.stop")}
          </button>
        )}
      </div>
    </div>
  );
}
