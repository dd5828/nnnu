"use client";

/** 用户消息气泡（右侧对齐）+ 消息上的引用 chips（§7.1）。
 *
 * `messageId`/`ordinal` 给会话进度条当锚点（ordinal 只有占格的消息才有——空消息
 * 没有刻度，进度条按 `[data-ordinal]` 筛正主）；`flashSeq` 是跳转命中后的闪烁
 * 序号，换个序号就重挂一次 overlay，让 CSS 动画从头播（气泡本身不 remount）。
 *
 * `refs` 是发送时快照（UiMessage.metadata），没引用的消息传共享空数组保住 memo。
 */

import { memo } from "react";
import MessageRefs from "./MessageRefs";
import { EMPTY_REFS, type RefEntry } from "@/lib/refs";

export interface UserMessageProps {
  content: string;
  refs?: RefEntry[];
  messageId?: string;
  ordinal?: number | null;
  flashSeq?: number | null;
}

function UserMessage({
  content,
  refs = EMPTY_REFS,
  messageId,
  ordinal = null,
  flashSeq = null,
}: UserMessageProps) {
  return (
    <div
      className="flex flex-col items-end"
      data-testid="user-message"
      data-message-id={messageId}
      data-ordinal={ordinal ?? undefined}
      data-flash={flashSeq !== null ? "true" : undefined}
    >
      <div className="relative max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
        {content}
        {flashSeq !== null && (
          <span
            key={flashSeq}
            aria-hidden
            className="rail-flash pointer-events-none absolute -inset-1 rounded-2xl"
          />
        )}
      </div>
      <MessageRefs refs={refs} />
    </div>
  );
}

// props 全是原始值（refs 靠共享空数组/合并复用保引用）：没变就不重渲
//（闪烁靠 flashSeq 变化打破 memo，照旧生效）
export default memo(UserMessage);
