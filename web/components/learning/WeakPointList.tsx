"use client";

/** 薄弱点（§7.5）：作答过但没达线的节点，一条一个深链——点进去就是题库页只看这个节点的题。
 *
 * 数据是后端算好的（`gap` = 离通过线还差多少），前端只负责画条与给路，
 * 不自己拿 mastery 和 gate 相减（两处算同一件事迟早会分叉）。
 */

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { masteryPercent, nodeTypeKey, weakPointHref } from "@/lib/learning";
import type { WeakPoint } from "@/types/api";

export default function WeakPointList({ points }: { points: WeakPoint[] }) {
  const { t } = useI18n();
  if (points.length === 0) {
    return <p className="text-xs text-muted">{t("learning.noWeakPoints")}</p>;
  }
  return (
    <ul className="space-y-1.5" data-testid="l-weak-list">
      {points.map((point) => (
        <li key={point.node_id} data-testid="l-weak" data-id={point.node_id}>
          <Link
            href={weakPointHref(point.node_id)}
            className="flex items-center gap-2 rounded-xl border border-border px-2.5 py-2 transition-colors hover:border-danger/40 hover:bg-danger/5"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-danger" />
            <span className="min-w-0 flex-1 truncate text-sm">{point.title}</span>
            <span className="shrink-0 rounded bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted">
              {t(nodeTypeKey(point.node_type))}
            </span>
            {/* 定量才有「差多少分」可讲；定性这里 gate 记的是 0，照抄会读成「40 / 0 分」 */}
            <span
              className="shrink-0 text-[11px] text-muted"
              data-testid="l-weak-gap"
              data-gate-kind={point.gate_kind}
            >
              {point.gate_kind === "quantitative"
                ? t("learning.gapLine", {
                    mastery: String(masteryPercent(point.mastery)),
                    gate: String(Math.round(point.gate)),
                    gap: String(Math.round(point.gap * 10) / 10),
                  })
                : `${t("learning.masteryLine", { n: String(masteryPercent(point.mastery)) })} · ${t(
                    "learning.gateQualitative"
                  )}`}
            </span>
            <span className="shrink-0 text-[11px] text-primary">{t("learning.practiceNode")}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
