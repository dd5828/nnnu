"use client";

/** 消息列表（§7.21）：历史消息 + 进行中回合。
 *
 * 滚动策略（2026-09-28 改）：新回合开始时把**回复的开头**对到视口顶，之后一律不跟——
 * 正文再长也不把视图往底下拽，读的人自己滚。（原先逐帧追底，回复的开头总被顶走。）
 * 两处例外：换会话落到最新一条；`ask_user` 的卡出现时要人点，必须拉到看得见。
 */

import { useEffect, useRef } from "react";
import { useChatStore } from "@/hooks/useChat";
import ActiveTurnView from "./ActiveTurnView";
import AssistantMessage from "./AssistantMessage";
import EmptyState from "./EmptyState";
import UserMessage from "./UserMessage";

/** 回合开头与视口顶之间留的白（px）：不贴着边，看着不局促 */
const TURN_TOP_GAP = 8;

export default function MessageList() {
  const messages = useChatStore((s) => s.messages);
  const active = useChatStore((s) => s.active);
  const sessionId = useChatStore((s) => s.sessionId);
  const containerRef = useRef<HTMLDivElement>(null);
  const turnRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // 换会话（历史装载）：落到最新一条
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [sessionId]);

  /** 视口顶到「进行中回合」顶部的距离（内容不够时算出来的值超大，浏览器会自己夹住） */
  const scrollToTurnTop = () => {
    const container = containerRef.current;
    const turn = turnRef.current;
    if (!container || !turn) {
      return;
    }
    const offset = turn.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTop += offset - TURN_TOP_GAP;
  };

  // 新回合：回复的开头对到视口顶，后面整屏都留给流式正文
  const turnId = active?.turnId ?? "";
  useEffect(() => {
    if (turnId) {
      scrollToTurnTop();
    }
  }, [turnId]);

  // ask_user 的卡：整张都露着就不动（别抢用户正在看的滚动位置），没露全拉到顶
  const askId = active?.askUser?.ask_id ?? "";
  useEffect(() => {
    if (!askId) {
      return;
    }
    const container = containerRef.current;
    // 卡片在 ActiveTurnView 里挂着，为一张临时卡把 ref 一路传下去不划算——按 testid 取
    const card = container?.querySelector<HTMLElement>('[data-testid="ask-card"]') ?? null;
    if (!container || !card) {
      return;
    }
    const box = container.getBoundingClientRect();
    const rect = card.getBoundingClientRect();
    if (rect.top >= box.top && rect.bottom <= box.bottom) {
      return;
    }
    card.scrollIntoView({ block: "start" });
  }, [askId]);

  if (!sessionId && messages.length === 0 && !active) {
    return <EmptyState />;
  }

  return (
    <div ref={containerRef} className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-6">
        {messages.map((message) =>
          message.role === "user" ? (
            <UserMessage key={message.id} content={message.content} />
          ) : (
            <AssistantMessage key={message.id} message={message} />
          )
        )}
        {active && (
          <div ref={turnRef}>
            <ActiveTurnView turn={active} />
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
