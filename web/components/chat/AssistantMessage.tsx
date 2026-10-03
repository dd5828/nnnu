"use client";

/** 历史 assistant 消息（§7.1）：思考块 + 正文 + 工具轨迹 + 引用 + 成本 + 存入笔记本 + 重新生成。
 *
 * 学习路径 / 研究各自多一个入口（去学习看板 / 确认大纲），按当前能力显示。
 */

import { memo } from "react";
import Link from "next/link";
import { Binoculars, Download, GraduationCap, RefreshCw } from "lucide-react";
import { useChatStore, type UiMessage } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { CAPABILITY_MASTERY, CAPABILITY_RESEARCH, isResearchOutline } from "@/lib/capabilities";
import { downloadMarkdown } from "@/lib/download";
import CitationsPanel from "./CitationsPanel";
import CostBadge from "./CostBadge";
import Markdown from "./Markdown";
import MasteryResultCard, { isGradedMasteryCall } from "./MasteryResultCard";
import SaveToNotebook from "./SaveToNotebook";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";

function AssistantMessage({ message }: { message: UiMessage }) {
  const { t } = useI18n();
  const regenerate = useChatStore((s) => s.regenerate);
  // 只要「是否忙碌」这一个布尔：流式 delta 期间它不变，历史消息整条不重渲
  const busy = useChatStore((s) => s.active !== null);
  const send = useChatStore((s) => s.send);
  const capability = useChatStore((s) => s.capability);
  const isLast = useChatStore((s) => s.messages[s.messages.length - 1]?.id === message.id);
  const isResearch = capability === CAPABILITY_RESEARCH;
  // 停在大纲上等答复的那一条：确认按钮只在这时出现（报告那一条不出现）
  const awaitingConfirm = isResearch && isLast && !busy && isResearchOutline(message.content);
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
        {isResearch && message.content && (
          <button
            type="button"
            data-testid="export-markdown"
            onClick={() => downloadMarkdown(message.content, t("chat.exportFileStem"))}
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <Download className="h-3 w-3" />
            {t("chat.exportMarkdown")}
          </button>
        )}
        {awaitingConfirm && (
          <button
            type="button"
            data-testid="research-confirm"
            onClick={() =>
              void send(t("chat.researchConfirmMessage"), [], { research_action: "confirm" })
            }
            className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-0.5 text-[11px] text-primary transition-colors hover:bg-primary/20"
          >
            <Binoculars className="h-3 w-3" />
            {t("chat.researchConfirm")}
          </button>
        )}
        {isLast && !busy && (
          <button
            type="button"
            data-testid="regenerate"
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

// 历史消息整条 memo：props 只有 message，引用没变（终局重取按 id 复用，见 lib/chat-messages）
// 就绝不重渲——流式期间不重渲，才是「历史多了也不卡」的关键
export default memo(AssistantMessage);
