"use client";

/** 消息列表（§7.21）：历史消息 + 进行中回合；自动滚动到底（用户上翻时不打扰）。 */

import { useEffect, useRef, useState } from "react";
import { useChatStore } from "@/hooks/useChat";
import ActiveTurnView from "./ActiveTurnView";
import AssistantMessage from "./AssistantMessage";
import EmptyState from "./EmptyState";
import UserMessage from "./UserMessage";

export default function MessageList() {
  const messages = useChatStore((s) => s.messages);
  const active = useChatStore((s) => s.active);
  const sessionId = useChatStore((s) => s.sessionId);
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const onScroll = () => {
      const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
      setStickToBottom(distance < 80);
    };
    container.addEventListener("scroll", onScroll);
    return () => container.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (stickToBottom) {
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
  }, [messages, active?.content, active?.thinking, stickToBottom]);

  if (!sessionId && messages.length === 0 && !active) {
    return <EmptyState />;
  }

  return (
    <div ref={containerRef} className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-6">
        {messages.map((message) =>
          message.role === "user" ? (
            <UserMessage key={message.id} content={message.content} />
          ) : (
            <AssistantMessage key={message.id} message={message} />
          )
        )}
        {active && <ActiveTurnView turn={active} />}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
