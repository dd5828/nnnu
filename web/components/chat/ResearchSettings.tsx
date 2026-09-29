"use client";

/** 研究设置行（§7.6）：只在能力选到「深度研究」时出现，两个小下拉。
 *
 * 档位（档次）决定拆几个子问题、每个查几轮；模式决定最后交出报告还是直接回答。
 * 值随每条消息的 config 下发（服务端 `TurnRequest.config` 直通），所以刷新/切会话后
 * 仍在——但**不落库**：这是这一档「怎么跑」的参数，不是会话的属性，换会话沿用当前选择
 * （与模型/知识库那种落库的粘性选择刻意不同，省得第二回合跑在一份用户已经改过的档位上）。
 *
 * 回合进行中禁用（与能力选择器同理）：档位是建调研时就写进草稿本的，跑到一半换掉只会
 * 让界面上的选择和实际跑的那一份对不上。
 */

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import {
  CAPABILITY_RESEARCH,
  RESEARCH_DEPTHS,
  RESEARCH_MODES,
  type ResearchDepth,
  type ResearchMode,
} from "@/lib/capabilities";

export default function ResearchSettings() {
  const { t } = useI18n();
  const capability = useChatStore((s) => s.capability);
  const depth = useChatStore((s) => s.researchDepth);
  const mode = useChatStore((s) => s.researchMode);
  const setDepth = useChatStore((s) => s.setResearchDepth);
  const setMode = useChatStore((s) => s.setResearchMode);
  const active = useChatStore((s) => s.active);
  const [open, setOpen] = useState<"depth" | "mode" | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(null);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  if (capability !== CAPABILITY_RESEARCH) {
    return null;
  }

  const pick = (apply: () => void) => {
    apply();
    setOpen(null);
  };

  return (
    <div ref={rootRef} className="flex items-center gap-1">
      <Dropdown
        testId="research-depth"
        value={depth}
        label={t(`chat.researchDepth_${depth}`)}
        title={t("chat.researchDepthHint")}
        disabled={Boolean(active)}
        open={open === "depth"}
        onToggle={() => setOpen((prev) => (prev === "depth" ? null : "depth"))}
      >
        {RESEARCH_DEPTHS.map((option) => (
          <Option
            key={option}
            testId="research-depth-option"
            value={option}
            selected={option === depth}
            label={t(`chat.researchDepth_${option}`)}
            description={t(`chat.researchDepthHint_${option}`)}
            onPick={() => pick(() => setDepth(option as ResearchDepth))}
          />
        ))}
      </Dropdown>
      <Dropdown
        testId="research-mode"
        value={mode}
        label={t(`chat.researchMode_${mode}`)}
        title={t("chat.researchModeHint")}
        disabled={Boolean(active)}
        open={open === "mode"}
        onToggle={() => setOpen((prev) => (prev === "mode" ? null : "mode"))}
      >
        {RESEARCH_MODES.map((option) => (
          <Option
            key={option}
            testId="research-mode-option"
            value={option}
            selected={option === mode}
            label={t(`chat.researchMode_${option}`)}
            description={t(`chat.researchModeHint_${option}`)}
            onPick={() => pick(() => setMode(option as ResearchMode))}
          />
        ))}
      </Dropdown>
    </div>
  );
}

function Dropdown({
  testId,
  value,
  label,
  title,
  disabled,
  open,
  onToggle,
  children,
}: {
  testId: string;
  value: string;
  label: string;
  title: string;
  disabled: boolean;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="relative">
      <button
        type="button"
        data-testid={testId}
        data-value={value}
        onClick={onToggle}
        disabled={disabled}
        aria-expanded={open}
        title={title}
        className="flex items-center gap-1 rounded-lg px-2 py-2 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className="truncate">{label}</span>
        <ChevronDown className="h-3 w-3 shrink-0" />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(260px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-lg">
          {children}
        </div>
      )}
    </div>
  );
}

function Option({
  testId,
  value,
  selected,
  label,
  description,
  onPick,
}: {
  testId: string;
  value: string;
  selected: boolean;
  label: string;
  description: string;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      data-value={value}
      onClick={onPick}
      className={`flex w-full items-start gap-2.5 px-3 py-2 text-left text-xs transition-colors ${
        selected ? "bg-primary/5" : "hover:bg-accent"
      }`}
    >
      <span className="min-w-0 flex-1">
        <span className="block font-medium">{label}</span>
        <span className="mt-0.5 block text-[11px] text-muted">{description}</span>
      </span>
      {selected && <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />}
    </button>
  );
}
