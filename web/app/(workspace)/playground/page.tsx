"use client";

/** 可视化实验场（§7.21 / §7.7）：左边提需求 + 快选渲染类型，右边看最新一帧。
 *
 * 复用聊天的 WS 机制（同一个 store），页面只做两件事：把能力锁在「可视化」上、
 * 把结果摆成「左边问右边看」的两栏。能力是**会话级粘性**的，所以进页面时若当前会话
 * 不是可视化会话就新建一个（聊天页那边的会话原样留着）；换会话会把在跑的回合甩掉，
 * 所以有回合进行中就不动。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { FlaskConical, Loader2, Send } from "lucide-react";
import Markdown from "@/components/chat/Markdown";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { apiFetch } from "@/lib/api";
import { CAPABILITY_VISUALIZE, VISUALIZE_RENDER_TYPES } from "@/lib/capabilities";

export default function PlaygroundPage() {
  const { t } = useI18n();
  const init = useChatStore((s) => s.init);
  const sessionId = useChatStore((s) => s.sessionId);
  const sessions = useChatStore((s) => s.sessions);
  const messages = useChatStore((s) => s.messages);
  // 细粒度订阅：流式 delta 只换 active 整对象，这里只要布尔与阶段文案
  const busy = useChatStore((s) => s.active !== null);
  const stage = useChatStore((s) => s.active?.stage ?? "");
  const send = useChatStore((s) => s.send);
  const stop = useChatStore((s) => s.stop);
  const setCapability = useChatStore((s) => s.setCapability);
  const renderType = useChatStore((s) => s.visualizeRenderType);
  const setRenderType = useChatStore((s) => s.setVisualizeRenderType);
  const [text, setText] = useState("");
  const [busyNotice, setBusyNotice] = useState(false);
  // 能力锁只跑一次（StrictMode 下 effect 会进两遍：用 ref 挡住第二次，且中途不取消，
  // 免得开发模式里两遍都半路退出、什么都没锁上）
  const lockStarted = useRef(false);
  const composingRef = useRef(false);

  useEffect(() => {
    init();
  }, [init]);

  // 能力锁：进页面就把能力拨到可视化；当前会话不是可视化会话且没回合在跑，就另起一个
  useEffect(() => {
    if (lockStarted.current) {
      return;
    }
    lockStarted.current = true;
    setCapability(CAPABILITY_VISUALIZE);
    void (async () => {
      const state = useChatStore.getState();
      if (state.active) {
        setBusyNotice(true);
        return;
      }
      if (state.sessionId) {
        try {
          const detail = await apiFetch<{ capability?: string }>(
            `/api/v1/sessions/${state.sessionId}`
          );
          if (detail.capability === CAPABILITY_VISUALIZE) {
            return;
          }
        } catch {
          // 会话没了/取不到：当作不是可视化会话，换一个
        }
      }
      await useChatStore.getState().newSession();
    })();
  }, [setCapability]);

  const lastAssistant = useMemo(() => {
    for (const message of [...messages].reverse()) {
      if (message.role === "assistant" && message.content) {
        return message.content;
      }
    }
    return "";
  }, [messages]);

  const sessionTitle = sessions.find((s) => s.id === sessionId)?.title ?? "";

  const submit = () => {
    if (!text.trim() || busy) {
      return;
    }
    const message = text;
    setText("");
    void send(message, []);
  };

  return (
    <main className="no-scrollbar flex min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto grid w-full max-w-5xl gap-4 px-4 py-6 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
        {/* 左栏：提问 + 渲染类型快选 */}
        <section className="flex flex-col gap-3">
          <div className="flex items-start gap-3">
            <div className="rounded-xl bg-accent p-2 text-primary">
              <FlaskConical className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <h1 className="text-xl font-semibold">{t("playground.title")}</h1>
              <p className="mt-0.5 text-xs text-muted">{t("playground.subtitle")}</p>
            </div>
          </div>

          <div className="rounded-2xl border border-border bg-surface p-3">
            <textarea
              value={text}
              data-testid="playground-input"
              onChange={(event) => setText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !composingRef.current) {
                  event.preventDefault();
                  submit();
                }
              }}
              onCompositionStart={() => {
                composingRef.current = true;
              }}
              onCompositionEnd={() => {
                composingRef.current = false;
              }}
              placeholder={t("playground.placeholder")}
              rows={3}
              className="w-full resize-none bg-transparent text-sm outline-none placeholder:text-muted"
            />
            <div className="mt-2 flex items-center justify-between gap-2">
              <span className="text-xs text-muted">
                {t(`chat.visualizeRenderType_${renderType}`)}
              </span>
              {busy ? (
                <button
                  type="button"
                  data-testid="playground-stop"
                  onClick={stop}
                  className="rounded-xl bg-danger/10 px-3 py-1.5 text-xs text-danger transition-colors hover:bg-danger/20"
                >
                  {t("chat.stop")}
                </button>
              ) : (
                <button
                  type="button"
                  data-testid="playground-send"
                  onClick={submit}
                  disabled={!text.trim()}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
                >
                  <Send className="h-3.5 w-3.5" />
                  {t("playground.send")}
                </button>
              )}
            </div>
          </div>

          <div>
            <p className="mb-1.5 text-xs text-muted">{t("chat.visualizeRenderType")}</p>
            <div className="flex flex-wrap gap-1.5">
              {VISUALIZE_RENDER_TYPES.map((option) => (
                <button
                  key={option}
                  type="button"
                  data-testid="playground-render-type"
                  data-value={option}
                  disabled={busy}
                  onClick={() => setRenderType(option)}
                  title={t(`chat.visualizeRenderTypeHint_${option}`)}
                  className={`rounded-full px-2.5 py-1 text-xs transition-colors disabled:opacity-50 ${
                    option === renderType
                      ? "bg-primary/10 text-primary"
                      : "bg-accent/60 text-muted hover:text-foreground"
                  }`}
                >
                  {t(`chat.visualizeRenderType_${option}`)}
                </button>
              ))}
            </div>
          </div>

          {busyNotice && (
            <p data-testid="playground-busy" className="text-xs text-muted">
              {t("playground.busy")}
            </p>
          )}
        </section>

        {/* 右栏：最新一帧（渲染块由 Markdown 里的 RenderViewer 负责画，全屏/下载都在那） */}
        <section className="flex min-w-0 flex-col gap-2">
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className="min-w-0 flex-1 truncate">{sessionTitle}</span>
            {busy && (
              <span
                data-testid="playground-status"
                className="inline-flex items-center gap-1.5 text-primary"
              >
                <Loader2 className="h-3 w-3 animate-spin" />
                {stage || t("playground.rendering")}
              </span>
            )}
          </div>
          <div className="min-h-[24rem] flex-1 rounded-2xl border border-border bg-surface p-3">
            {lastAssistant ? (
              <Markdown text={lastAssistant} />
            ) : (
              <p
                data-testid="playground-empty"
                className="flex h-full items-center justify-center py-10 text-center text-sm text-muted"
              >
                {t("playground.empty")}
              </p>
            )}
          </div>
          <p className="text-[11px] text-muted">{t("playground.hint")}</p>
        </section>
      </div>
    </main>
  );
}
