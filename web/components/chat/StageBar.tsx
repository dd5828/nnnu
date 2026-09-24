"use client";

/** 阶段步骤条（§7.3）：多阶段能力（deep_solve）依次点亮 规划 → 推导 → 书写。
 *
 * 只用既有 `status` 事件（payload 已是 {stage,message}），不新增事件类型：
 * 已过阶段打勾、当前转圈、未到灰显。不认识的能力（阶段表里没有）就按**观测到的**
 * 阶段顺序渲染——降级不炸，也照样能看出跑到哪了。
 */

import { Check, Circle, Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { CAPABILITY_LABEL_KEYS, STAGE_LABEL_KEYS, STAGES_BY_CAPABILITY } from "@/lib/capabilities";

export default function StageBar({ capability, stages }: { capability: string; stages: string[] }) {
  const { t } = useI18n();
  // 阶段表里有就按声明顺序（含还没到的灰点）；没有就只渲染观测到的
  const declared = STAGES_BY_CAPABILITY[capability] ?? [];
  const keys = declared.length > 0 ? declared : stages;
  if (keys.length === 0) {
    return null;
  }

  if (keys.length === 1) {
    // 单阶段能力（chat）：没有阶段可言，留一个转圈的当前状态就够（§7.21）
    return (
      <span
        data-testid="stage-pill"
        data-stage={keys[0]}
        data-state="active"
        className="inline-flex items-center gap-1.5 rounded-full bg-accent/60 px-2.5 py-0.5 text-[11px] text-muted"
      >
        <Loader2 className="h-3 w-3 animate-spin" />
        {t(CAPABILITY_LABEL_KEYS[capability] ?? "") || keys[0]}
      </span>
    );
  }

  const reached = stages[stages.length - 1];
  const currentIndex = reached ? keys.indexOf(reached) : -1;

  return (
    <div data-testid="stage-bar" className="flex flex-wrap items-center gap-1.5">
      {keys.map((key, index) => {
        const state =
          currentIndex < 0
            ? "pending"
            : index < currentIndex
              ? "done"
              : index === currentIndex
                ? "active"
                : "pending";
        const label = t(STAGE_LABEL_KEYS[key] ?? "") || key;
        return (
          <span key={key} className="flex items-center gap-1.5">
            {index > 0 && <span className="h-px w-4 bg-border" />}
            <span
              data-testid="stage-pill"
              data-stage={key}
              data-state={state}
              className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] transition-colors ${
                state === "active"
                  ? "bg-primary/10 text-primary"
                  : state === "done"
                    ? "bg-accent/60 text-foreground"
                    : "bg-accent/40 text-muted"
              }`}
            >
              {state === "active" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : state === "done" ? (
                <Check className="h-3 w-3" />
              ) : (
                <Circle className="h-2.5 w-2.5" />
              )}
              {label}
            </span>
          </span>
        );
      })}
    </div>
  );
}
