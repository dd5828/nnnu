"use client";

/** Chat 工作台（§7.21 首页）：会话列表 + 消息区 + 输入区；顶部状态条（连接/会话标题）。 */

import { useEffect, useState } from "react";
import { PanelLeft } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import Composer from "./Composer";
import MessageList from "./MessageList";
import SessionList from "./SessionList";

export default function ChatWorkspace() {
  const { t } = useI18n();
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const init = useChatStore((s) => s.init);
  const socketStatus = useChatStore((s) => s.socketStatus);
  const sessionId = useChatStore((s) => s.sessionId);
  const sessions = useChatStore((s) => s.sessions);
  const topError = useChatStore((s) => s.topError);
  const clearTopError = useChatStore((s) => s.clearTopError);

  useEffect(() => {
    init();
  }, [init]);

  const title = sessions.find((s) => s.id === sessionId)?.title ?? "";

  return (
    <div className="flex min-h-0 flex-1">
      <SessionList open={sessionsOpen} onClose={() => setSessionsOpen(false)} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-border/70 px-4 py-2">
          {/* 窄屏才出现：会话列表同样收进了抽屉（≥ md 常驻，这个按钮不渲染） */}
          <button
            type="button"
            data-testid="chat-sessions-toggle"
            onClick={() => setSessionsOpen((value) => !value)}
            className="-ml-1.5 shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground md:hidden"
            aria-label={t("chat.sessionList")}
            aria-expanded={sessionsOpen}
            aria-controls="chat-sessions"
            title={t("chat.sessionList")}
          >
            <PanelLeft className="h-4 w-4" />
          </button>
          {title && <span className="truncate text-sm font-medium">{title}</span>}
          <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-muted">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                socketStatus === "open"
                  ? "bg-success"
                  : socketStatus === "closed"
                    ? "bg-muted"
                    : "animate-pulse bg-yellow-500"
              }`}
            />
            {socketStatus === "open"
              ? t("chat.connected")
              : socketStatus === "closed"
                ? t("chat.closed")
                : t("chat.reconnecting")}
          </span>
        </div>
        {topError && (
          <div className="flex items-center justify-between gap-2 border-b border-danger/30 bg-danger/10 px-4 py-2 text-xs text-danger">
            <span className="truncate">{topError}</span>
            <button type="button" onClick={clearTopError} className="shrink-0 underline">
              {t("common.close")}
            </button>
          </div>
        )}
        <MessageList />
        <Composer />
      </div>
    </div>
  );
}
