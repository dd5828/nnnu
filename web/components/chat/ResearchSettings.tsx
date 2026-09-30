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
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import {
  CAPABILITY_RESEARCH,
  RESEARCH_DEPTHS,
  RESEARCH_MODES,
  type ResearchDepth,
  type ResearchMode,
} from "@/lib/capabilities";
import { ToolbarDropdown, ToolbarOption } from "./ToolbarDropdown";

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
      <ToolbarDropdown
        testId="research-depth"
        value={depth}
        label={t(`chat.researchDepth_${depth}`)}
        title={t("chat.researchDepthHint")}
        disabled={Boolean(active)}
        open={open === "depth"}
        onToggle={() => setOpen((prev) => (prev === "depth" ? null : "depth"))}
      >
        {RESEARCH_DEPTHS.map((option) => (
          <ToolbarOption
            key={option}
            testId="research-depth-option"
            value={option}
            selected={option === depth}
            label={t(`chat.researchDepth_${option}`)}
            description={t(`chat.researchDepthHint_${option}`)}
            onPick={() => pick(() => setDepth(option as ResearchDepth))}
          />
        ))}
      </ToolbarDropdown>
      <ToolbarDropdown
        testId="research-mode"
        value={mode}
        label={t(`chat.researchMode_${mode}`)}
        title={t("chat.researchModeHint")}
        disabled={Boolean(active)}
        open={open === "mode"}
        onToggle={() => setOpen((prev) => (prev === "mode" ? null : "mode"))}
      >
        {RESEARCH_MODES.map((option) => (
          <ToolbarOption
            key={option}
            testId="research-mode-option"
            value={option}
            selected={option === mode}
            label={t(`chat.researchMode_${option}`)}
            description={t(`chat.researchModeHint_${option}`)}
            onPick={() => pick(() => setMode(option as ResearchMode))}
          />
        ))}
      </ToolbarDropdown>
    </div>
  );
}
