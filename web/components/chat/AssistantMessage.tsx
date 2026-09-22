"use client";

/** 历史 assistant 消息（§7.1）：思考块 + 正文 + 工具轨迹 + 引用 + 成本 + 重新生成。 */

import { RefreshCw } from "lucide-react";
import { useChatStore, type UiMessage } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import CitationsPanel from "./CitationsPanel";
import CostBadge from "./CostBadge";
import Markdown from "./Markdown";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";

export default function AssistantMessage({ message }: { message: UiMessage }) {
  const { t } = useI18n();
  const regenerate = useChatStore((s) => s.regenerate);
  const active = useChatStore((s) => s.active);
  const isLast = useChatStore((s) => s.messages[s.messages.length - 1]?.id === message.id);
  return (
    <div className="flex flex-col items-start gap-1.5">
      {message.thinking && <ThinkingBlock text={message.thinking} streaming={false} />}
      {message.content && (
        <div className="max-w-full">
          <Markdown text={message.content} />
        </div>
      )}
      {message.tool_calls.map((call) => (
        <ToolCallCard key={call.call_id} call={call} />
      ))}
      {message.citations.length > 0 && <CitationsPanel sources={message.citations} />}
      <div className="flex items-center gap-2">
        {message.cost && <CostBadge tokens={message.cost.tokens} cost={message.cost.cost} />}
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
