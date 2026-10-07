"use client";

/** 页聊天面板（§7.14 验收「页码对话能引用来源文档」）：非流式一问一答，
 *  历史由服务端存本页（页详情带回，这里只渲染 props）；回答带引用面板。
 *
 * 输入框是表单：回车即问（无 Shift+Enter 换行需求——它本来就问一句话）。
 * 知识库没就绪时后端如实降级，这里把 degraded 提示挂在最近一次回答下。
 */

import { useEffect, useRef, useState } from "react";
import { Loader2, Send } from "lucide-react";
import CitationsPanel from "@/components/chat/CitationsPanel";
import Markdown from "@/components/chat/Markdown";
import { useBookChat } from "@/hooks/useBook";
import { useI18n } from "@/hooks/useI18n";
import { errorText } from "@/lib/errors";
import type { BookPageMessage } from "@/types/api";

interface Props {
  bookId: string;
  pageId: string;
  chapterTitle: string;
  messages: BookPageMessage[];
}

export default function PageChatPanel({ bookId, pageId, chapterTitle, messages }: Props) {
  const { t } = useI18n();
  const chat = useBookChat(bookId, pageId);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // 新消息/新回答落在底部就滚到底（DOM 滚动，不触发状态更新）
  useEffect(() => {
    const host = listRef.current;
    if (host) {
      host.scrollTop = host.scrollHeight;
    }
  }, [messages.length, chat.isPending]);

  const send = () => {
    const question = input.trim();
    if (!question || chat.isPending) {
      return;
    }
    setError(null);
    setInput("");
    chat.mutate(question, {
      onError: (err) => setError(errorText(err, t("common.requestFailed"))),
    });
  };

  return (
    <div
      data-testid="bk-chat"
      className="flex min-h-0 flex-1 flex-col rounded-xl border border-border bg-surface/60"
    >
      <div className="border-b border-border px-3 py-2">
        <p className="text-xs font-medium">{t("book.chatTitle")}</p>
        <p className="truncate text-[11px] text-muted">{chapterTitle}</p>
      </div>

      <div
        ref={listRef}
        className="no-scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3"
      >
        {messages.length === 0 && !chat.isPending && (
          <p className="text-xs text-muted">{t("book.chatEmpty")}</p>
        )}
        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.id} data-testid="bk-chat-message" data-role="user">
              <p className="ml-6 rounded-lg bg-accent/60 px-2.5 py-1.5 text-xs">
                {message.content_md}
              </p>
            </div>
          ) : (
            <div key={message.id} data-testid="bk-chat-message" data-role="assistant">
              <div className="text-xs">
                <Markdown text={message.content_md} />
              </div>
              {message.citations.length > 0 && <CitationsPanel sources={message.citations} />}
            </div>
          )
        )}
        {chat.isPending && (
          <p
            data-testid="bk-chat-busy"
            className="inline-flex items-center gap-2 text-xs text-muted"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t("book.chatBusy")}
          </p>
        )}
        {chat.isSuccess && chat.data?.degraded && (
          <p className="text-[11px] text-muted">{t("book.chatDegraded")}</p>
        )}
      </div>

      <form
        className="border-t border-border p-2"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        <div className="flex items-center gap-1.5">
          <input
            data-testid="bk-chat-input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={t("book.chatPlaceholder")}
            className="min-w-0 flex-1 rounded-lg border border-border bg-transparent px-2.5 py-1.5 text-xs outline-none focus:border-primary/50"
          />
          <button
            type="submit"
            data-testid="bk-chat-send"
            disabled={chat.isPending || !input.trim()}
            aria-label={t("book.chatSend")}
            className="shrink-0 rounded-lg bg-primary p-1.5 text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
          >
            {chat.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
        {error && <p className="mt-1 text-[11px] text-danger">{error}</p>}
      </form>
    </div>
  );
}
