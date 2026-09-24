"use client";

/** 模型选择器（§6.10，对齐上游 DeepTutor）：会话级粘性 + 单选，默认跟随设置。
 *
 * 选项来自后端注册表快照（不探测、不支持自由输入）；首行「跟随设置默认」= 清空会话选择，
 * 回到设置页/环境变量定的那个模型。选择随每条消息全量下发给后端落库，所以切会话/刷新后仍在。
 */

import { useEffect, useRef, useState } from "react";
import { Bot, Check } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { useLlmOptions } from "@/hooks/useSettings";
import type { LlmOption } from "@/types/api";

/** 行右侧的小字：provider 名 + 上下文窗口（131072 → 128K）。 */
function metaLabel(option: LlmOption): string {
  const context = option.context_window ? `${Math.round(option.context_window / 1024)}K` : "";
  return [option.provider_label, context].filter(Boolean).join(" · ");
}

export default function ModelSelector() {
  const { t } = useI18n();
  const modelRef = useChatStore((s) => s.modelRef);
  const setModelRef = useChatStore((s) => s.setModelRef);
  const { data } = useLlmOptions();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const options = data?.options ?? [];
  const active = data?.active ?? null;
  const current = options.find((option) => option.value === modelRef);
  // 不认识的存值（注册表里已删掉的模型）原样显示，别静默伪装成「跟随默认」
  const label = modelRef === null ? t("chat.followDefault") : current?.label ?? modelRef;

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

  const pick = (value: string | null) => {
    setModelRef(value);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="model-selector"
        onClick={() => setOpen((prev) => !prev)}
        aria-label={t("chat.selectModel")}
        aria-expanded={open}
        title={t("chat.modelHint")}
        className={`flex max-w-[10rem] items-center gap-1.5 rounded-lg p-2 text-xs transition-colors ${
          modelRef !== null
            ? "text-primary hover:bg-primary/10"
            : "text-muted hover:bg-accent hover:text-foreground"
        }`}
      >
        <Bot className="h-4 w-4 shrink-0" />
        <span className="truncate">{label}</span>
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(320px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface shadow-lg">
          {options.length === 0 ? (
            <div className="px-3 py-4 text-center text-xs text-muted">{t("chat.modelEmpty")}</div>
          ) : (
            <div className="max-h-[300px] overflow-y-auto py-1">
              <button
                type="button"
                data-testid="model-option-default"
                onClick={() => pick(null)}
                className={`flex w-full flex-col items-start gap-0.5 px-3 py-1.5 text-left text-xs transition-colors ${
                  modelRef === null ? "bg-primary/5" : "hover:bg-accent"
                }`}
              >
                <span className="flex w-full items-center gap-2.5">
                  <span className="min-w-0 flex-1 truncate font-medium">
                    {t("chat.followDefault")}
                  </span>
                  {modelRef === null && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                </span>
                {active && (
                  <span className="w-full truncate text-[11px] text-muted">
                    {t("chat.modelCurrent", { model: active })}
                  </span>
                )}
              </button>

              {options.map((option) => {
                const selected = modelRef === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    data-testid="model-option"
                    data-value={option.value}
                    disabled={option.missing_key}
                    title={option.missing_key ? t("chat.modelMissingKey") : option.value}
                    onClick={() => pick(option.value)}
                    className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-xs transition-colors ${
                      option.missing_key
                        ? "cursor-not-allowed opacity-50"
                        : selected
                          ? "bg-primary/5"
                          : "hover:bg-accent"
                    }`}
                  >
                    <Bot
                      className={`h-3.5 w-3.5 shrink-0 ${selected ? "text-primary" : "text-muted"}`}
                    />
                    <span className="min-w-0 flex-1 truncate font-medium">{option.label}</span>
                    {option.is_active_default && (
                      <span className="shrink-0 rounded bg-accent px-1 py-0.5 text-[10px] text-muted">
                        {t("chat.modelDefaultTag")}
                      </span>
                    )}
                    <span className="shrink-0 text-[10px] text-muted">{metaLabel(option)}</span>
                    {selected && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
