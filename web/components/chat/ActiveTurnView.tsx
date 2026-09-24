"use client";

/** 进行中回合（§6.1 事件流实时渲染）：阶段条 + 思考 + 正文 + 工具 + 引用 + ask_user + 成本。 */

import { CircleStop, Loader2 } from "lucide-react";
import { useChatStore, type ActiveTurn } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import CitationsPanel from "./CitationsPanel";
import CostBadge from "./CostBadge";
import Markdown, { useDebouncedText } from "./Markdown";
import StageBar from "./StageBar";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";

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
      {Object.values(turn.toolCalls).map((call) => (
        <ToolCallCard key={call.call_id} call={call} />
      ))}
      {turn.citations.length > 0 && <CitationsPanel sources={turn.citations} />}
      {turn.warning && (
        <div className="w-full rounded-lg border border-border/70 bg-accent/40 px-3 py-2 text-xs text-muted">
          {turn.warning}
        </div>
      )}
      {turn.askUser && (
        <div className="w-full rounded-xl border border-primary/30 bg-surface p-3">
          <div className="mb-2 text-sm font-medium">{turn.askUser.question}</div>
          <div className="flex flex-wrap gap-2">
            {turn.askUser.options.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => replyAskUser(option)}
                className="rounded-lg border border-border bg-accent/50 px-3 py-1.5 text-sm transition-colors hover:bg-accent"
              >
                {option}
              </button>
            ))}
          </div>
        </div>
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
