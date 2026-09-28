"use client";

/** 到期复习聚合（§7.5）：跨路径的「该复习了」，摆在路径列表顶上。
 *
 * 单条路径的复习安排在看板详情里（`ReviewList`）；这里回答的是另一个问题——
 * 手上好几条路径时，今天该先摸哪几道。数据来自 `GET /learning/reviews`（本页唯一
 * 一个跨路径的读端点），排序与「是否到期」都由后端给，前端只做展示。
 *
 * 没有到期项就**整块不渲染**：列表页不留一个空盒子。点一行进对应路径的看板并选中
 * 那个节点（`nodeHref` 走 query，深链可分享）。
 */

import Link from "next/link";
import { CalendarClock } from "lucide-react";
import { useDueReviews } from "@/hooks/useLearning";
import { useI18n } from "@/hooks/useI18n";
import { useNow } from "@/hooks/useNow";
import { nodeHref, nodeTypeKey, reviewWhen } from "@/lib/learning";

export default function DueReviewBoard() {
  const { t } = useI18n();
  const now = useNow();
  const { data } = useDueReviews();
  const items = data?.reviews ?? [];
  if (items.length === 0) {
    return null;
  }
  const overdue = items.filter((item) => item.overdue).length;

  return (
    <section
      data-testid="l-due-board"
      data-total={items.length}
      data-overdue={overdue}
      className="flex flex-col gap-2 rounded-xl border border-danger/30 bg-danger/[0.03] p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <CalendarClock className="h-4 w-4 text-danger" />
        <h2 className="text-sm font-medium">{t("learning.dueReviews")}</h2>
        <span className="text-[11px] text-muted">
          {t("learning.dueSummary", {
            n: String(items.length),
            overdue: String(overdue),
          })}
        </span>
      </div>
      <ul className="space-y-1" data-testid="l-due-list">
        {items.map((item) => {
          const when = reviewWhen(item.next_review_at, now);
          return (
            <li
              key={`${item.path_id}:${item.node_id}`}
              data-testid="l-due-item"
              data-path-id={item.path_id}
              data-node-id={item.node_id}
              data-overdue={item.overdue}
            >
              <Link
                href={nodeHref(item.path_id, item.node_id)}
                className="flex flex-wrap items-center gap-2 rounded-lg px-2 py-1.5 text-xs transition-colors hover:bg-accent/60"
              >
                <span className="shrink-0 rounded bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted">
                  {item.path_title}
                </span>
                <span className="min-w-0 flex-1 truncate">{item.title}</span>
                <span className="shrink-0 text-[10px] text-muted">
                  {t(nodeTypeKey(item.node_type))}
                </span>
                <span className="shrink-0 text-[10px] text-muted">
                  {t("learning.stageLine", { n: String(item.review_stage + 1) })}
                </span>
                <span
                  className={`shrink-0 text-[11px] ${item.overdue ? "text-danger" : "text-muted"}`}
                >
                  {t(when.key, when.vars)}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
