"use client";

/** 用户消息气泡（右侧对齐）。
 *
 * `messageId`/`ordinal` 给会话进度条当锚点（ordinal 只有占格的消息才有——空消息
 * 没有刻度，进度条按 `[data-ordinal]` 筛正主）；`flashSeq` 是跳转命中后的闪烁
 * 序号，换个序号就重挂一次 overlay，让 CSS 动画从头播（气泡本身不 remount）。
 */

import { memo } from "react";

export interface UserMessageProps {
  content: string;
  messageId?: string;
  ordinal?: number | null;
  flashSeq?: number | null;
}

function UserMessage({ content, messageId, ordinal = null, flashSeq = null }: UserMessageProps) {
  return (
    <div
      className="flex justify-end"
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
    </div>
  );
}

// props 全是原始值：内容没变就不重渲（闪烁靠 flashSeq 变化打破 memo，照旧生效）
export default memo(UserMessage);
