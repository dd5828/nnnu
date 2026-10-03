"use client";

/** 数学动画设置行（§7.8）：只在能力选到「数学动画」时出现，一个画质下拉。
 *
 * 画质对应 manim 的 -ql/-qm/-qh，直接决定渲染时长，所以摆在输入区而不是设置页——
 * 这是「这一条动画想等多久」的选择。值随 config 下发、不落库；回合中禁用（渲染参数
 * 在代码生成前就定死了）。
 */

import { useEffect, useRef, useState } from "react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import {
  ANIMATOR_QUALITIES,
  CAPABILITY_MATH_ANIMATOR,
  type AnimatorQuality,
} from "@/lib/capabilities";
import { ToolbarDropdown, ToolbarOption } from "./ToolbarDropdown";

export default function MathAnimatorSettings() {
  const { t } = useI18n();
  const capability = useChatStore((s) => s.capability);
  const quality = useChatStore((s) => s.animatorQuality);
  const setQuality = useChatStore((s) => s.setAnimatorQuality);
  const busy = useChatStore((s) => s.active !== null);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

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

  if (capability !== CAPABILITY_MATH_ANIMATOR) {
    return null;
  }

  return (
    <div ref={rootRef} className="flex items-center gap-1">
      <ToolbarDropdown
        testId="animator-quality"
        value={quality}
        label={t(`chat.animatorQuality_${quality}`)}
        title={t("chat.animatorQualityHint")}
        disabled={busy}
        open={open}
        onToggle={() => setOpen((prev) => !prev)}
      >
        {ANIMATOR_QUALITIES.map((option) => (
          <ToolbarOption
            key={option}
            testId="animator-quality-option"
            value={option}
            selected={option === quality}
            label={t(`chat.animatorQuality_${option}`)}
            description={t(`chat.animatorQualityHint_${option}`)}
            onPick={() => {
              setQuality(option as AnimatorQuality);
              setOpen(false);
            }}
          />
        ))}
      </ToolbarDropdown>
    </div>
  );
}
