"use client";

/** 能力选择器（§6.4）：会话级粘性——本会话后续回合都走选中的能力。
 *
 * 与模型选择（§6.10）同款：随每条消息全量下发给后端落库，切会话时水合回来。
 * 回合进行中禁用：能力决定整个回合的流水线，跑一半换掉只会让上下文对不上。
 */

import { useEffect, useRef, useState } from "react";
import { Check, MessagesSquare, Sigma } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import {
  CAPABILITY_CHAT,
  CAPABILITY_HINT_KEYS,
  CAPABILITY_LABEL_KEYS,
  CAPABILITY_SOLVE,
} from "@/lib/capabilities";

const OPTIONS = [
  { value: CAPABILITY_CHAT, icon: MessagesSquare },
  { value: CAPABILITY_SOLVE, icon: Sigma },
];

export default function CapabilitySelector() {
  const { t } = useI18n();
  const capability = useChatStore((s) => s.capability);
  const setCapability = useChatStore((s) => s.setCapability);
  const active = useChatStore((s) => s.active);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const current = OPTIONS.find((option) => option.value === capability) ?? OPTIONS[0];
  const CurrentIcon = current.icon;

  useEffect(() => {
    if (!open) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const pick = (value: string) => {
    setCapability(value);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="capability-selector"
        data-value={capability}
        onClick={() => setOpen((prev) => !prev)}
        disabled={Boolean(active)}
        aria-label={t("chat.selectCapability")}
        aria-expanded={open}
        title={active ? t("chat.capabilityBusy") : t("chat.capabilityHint")}
        className={`flex max-w-[10rem] items-center gap-1.5 rounded-lg p-2 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
          capability !== CAPABILITY_CHAT
            ? "text-primary hover:bg-primary/10"
            : "text-muted hover:bg-accent hover:text-foreground"
        }`}
      >
        <CurrentIcon className="h-4 w-4 shrink-0" />
        <span className="truncate">
          {t(CAPABILITY_LABEL_KEYS[capability] ?? "chat.capabilityChat")}
        </span>
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(320px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-lg">
          {OPTIONS.map((option) => {
            const Icon = option.icon;
            const selected = capability === option.value;
            return (
              <button
                key={option.value}
                type="button"
                data-testid="capability-option"
                data-value={option.value}
                onClick={() => pick(option.value)}
                className={`flex w-full items-start gap-2.5 px-3 py-2 text-left text-xs transition-colors ${
                  selected ? "bg-primary/5" : "hover:bg-accent"
                }`}
              >
                <Icon
                  className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${
                    selected ? "text-primary" : "text-muted"
                  }`}
                />
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">
                    {t(CAPABILITY_LABEL_KEYS[option.value])}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-muted">
                    {t(CAPABILITY_HINT_KEYS[option.value])}
                  </span>
                </span>
                {selected && <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
