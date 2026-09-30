"use client";

/** 可视化设置行（§7.7）：只在能力选到「可视化」时出现，一个渲染类型下拉。
 *
 * 默认 auto——交给分析段按需求挑类型（§7.7 的路由规则在提示词里）；用户点了具体类型
 * 就 pin 住，模型不许改（prompt 注入「用户已指定渲染类型」）。值随 config 下发、不落库，
 * 与研究档位同款。回合进行中禁用：pin 是在分析段就生效的，跑到一半改只会对不上。
 */

import { useEffect, useRef, useState } from "react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import {
  CAPABILITY_VISUALIZE,
  VISUALIZE_RENDER_TYPES,
  type VisualizeRenderType,
} from "@/lib/capabilities";
import { ToolbarDropdown, ToolbarOption } from "./ToolbarDropdown";

export default function VisualizeSettings() {
  const { t } = useI18n();
  const capability = useChatStore((s) => s.capability);
  const renderType = useChatStore((s) => s.visualizeRenderType);
  const setRenderType = useChatStore((s) => s.setVisualizeRenderType);
  const active = useChatStore((s) => s.active);
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

  if (capability !== CAPABILITY_VISUALIZE) {
    return null;
  }

  return (
    <div ref={rootRef} className="flex items-center gap-1">
      <ToolbarDropdown
        testId="visualize-render-type"
        value={renderType}
        label={t(`chat.visualizeRenderType_${renderType}`)}
        title={t("chat.visualizeRenderTypeHint")}
        disabled={Boolean(active)}
        open={open}
        onToggle={() => setOpen((prev) => !prev)}
      >
        {VISUALIZE_RENDER_TYPES.map((option) => (
          <ToolbarOption
            key={option}
            testId="visualize-render-type-option"
            value={option}
            selected={option === renderType}
            label={t(`chat.visualizeRenderType_${option}`)}
            description={t(`chat.visualizeRenderTypeHint_${option}`)}
            onPick={() => {
              setRenderType(option as VisualizeRenderType);
              setOpen(false);
            }}
          />
        ))}
      </ToolbarDropdown>
    </div>
  );
}
