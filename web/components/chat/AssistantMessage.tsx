"use client";

/** 历史 assistant 消息（§7.1）：思考块 + 正文 + 工具轨迹 + 引用 + 成本 + 存入笔记本 + 重新生成。
 *
 * 出题会话（§7.4）多一个「去题库作答」入口：作答判分在题库页做，聊天里只给路。
 */

import Link from "next/link";
import { GraduationCap, ListChecks, RefreshCw } from "lucide-react";
import { useChatStore, type UiMessage } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { CAPABILITY_MASTERY, CAPABILITY_QUESTION } from "@/lib/capabilities";
import CitationsPanel from "./CitationsPanel";
import CostBadge from "./CostBadge";
import Markdown from "./Markdown";
import MasteryResultCard, { isGradedMasteryCall } from "./MasteryResultCard";
import SaveToNotebook from "./SaveToNotebook";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";

export default function AssistantMessage({ message }: { message: UiMessage }) {
  const { t } = useI18n();
  const regenerate = useChatStore((s) => s.regenerate);
  const active = useChatStore((s) => s.active);
  const capability = useChatStore((s) => s.capability);
  const isLast = useChatStore((s) => s.messages[s.messages.length - 1]?.id === message.id);
  return (
    <div className="flex flex-col items-start gap-1.5">
      {message.thinking && <ThinkingBlock text={message.thinking} streaming={false} />}
      {message.content && (
        <div className="max-w-full">
          <Markdown text={message.content} />
        </div>
      )}
      {message.tool_calls.map((call) =>
        // 判过分的答题调用摊成结果卡；其余（含失败的）走通用折叠卡
        isGradedMasteryCall(call) ? (
          <MasteryResultCard key={call.call_id} call={call} />
        ) : (
          <ToolCallCard key={call.call_id} call={call} />
        )
      )}
      {message.citations.length > 0 && <CitationsPanel sources={message.citations} />}
      <div className="flex items-center gap-2">
        {message.cost && <CostBadge tokens={message.cost.tokens} cost={message.cost.cost} />}
        {message.content && <SaveToNotebook content={message.content} />}
        {message.content && capability === CAPABILITY_QUESTION && (
          <Link
            href="/questions"
            data-testid="go-to-questions"
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-primary transition-colors hover:bg-primary/10"
          >
            <ListChecks className="h-3 w-3" />
            {t("chat.goToQuestions")}
          </Link>
        )}
        {message.content && capability === CAPABILITY_MASTERY && (
          <Link
            href="/learning"
            data-testid="go-to-learning"
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-primary transition-colors hover:bg-primary/10"
          >
            <GraduationCap className="h-3 w-3" />
            {t("chat.goToLearning")}
          </Link>
        )}
        {isLast && !active && (
          <button
            type="button"
            onClick={regenerate}
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <RefreshCw className="h-3 w-3" />
            {t("chat.regenerate")}
          </button>
        )}
      </div>
    </div>
  );
}
