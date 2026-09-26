"use client";

/** 复习建议（§7.5）：节点按下次复习时间排，到期的标红并排在最前。
 *
 * 排序与「是否到期」都由后端给（`overdue` + 列表顺序），这里只做展示——
 * 到期与否还牵涉服务端时间与四态判定，前端再算一遍只会多一个分歧点。
 */

import { CalendarClock } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { nodeTypeKey, reviewWhen } from "@/lib/learning";
import type { ReviewItem } from "@/types/api";

export default function ReviewList({ items, now }: { items: ReviewItem[]; now: number }) {
  const { t } = useI18n();
  if (items.length === 0) {
    return <p className="text-xs text-muted">{t("learning.noReviews")}</p>;
  }
  return (
    <ul className="space-y-1.5" data-testid="l-review-list">
      {items.map((item) => {
        const when = reviewWhen(item.next_review_at, now);
        return (
          <li
            key={item.node_id}
            data-testid="l-review"
            data-id={item.node_id}
            data-overdue={item.overdue}
            className={`flex items-center gap-2 rounded-xl border px-2.5 py-2 ${
              item.overdue ? "border-danger/40 bg-danger/5" : "border-border"
            }`}
          >
            <CalendarClock
              className={`h-3.5 w-3.5 shrink-0 ${item.overdue ? "text-danger" : "text-muted"}`}
            />
            <span className="min-w-0 flex-1 truncate text-sm">{item.title}</span>
            <span className="shrink-0 rounded bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted">
              {t(nodeTypeKey(item.node_type))}
            </span>
            <span className="shrink-0 text-[11px] text-muted">
              {t("learning.stageLine", { n: String(item.review_stage + 1) })}
            </span>
            <span className={`shrink-0 text-[11px] ${item.overdue ? "text-danger" : "text-muted"}`}>
              {t(when.key, when.vars)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
