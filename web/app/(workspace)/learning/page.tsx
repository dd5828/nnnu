"use client";

/** 学习看板列表（§7.5）：到期复习聚合 + 路径卡片 + 新建/去聊天入口。
 *
 * 路径**仍然只在聊天里长出来**（拍板 #3：没有 POST /learning/paths）：这里的「新建」
 * 只是开一个学习会话（零 LLM）把用户送进聊天去说第一句，卡片上的「去聊天」是切到
 * 某条路径的会话（`useOpenPathChat`）。
 */

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { GraduationCap, Loader2, MessageSquare, Plus } from "lucide-react";
import DueReviewBoard from "@/components/learning/DueReviewBoard";
import { useChatStore } from "@/hooks/useChat";
import { useLearningPaths, useOpenPathChat } from "@/hooks/useLearning";
import { useI18n } from "@/hooks/useI18n";
import { useNow } from "@/hooks/useNow";
import { CAPABILITY_MASTERY } from "@/lib/capabilities";
import { nextActionKey, pathHref, reviewWhen } from "@/lib/learning";
import type { LearningPathCard } from "@/types/api";

function PathCard({
  card,
  now,
  onOpen,
  opening,
}: {
  card: LearningPathCard;
  now: number;
  onOpen: (id: string) => void;
  opening: boolean;
}) {
  const { t } = useI18n();
  // 进度 = 已过门 / 总数（不再随时间往回掉：到期待复习仍算已掌握）
  const percent = Math.round(card.stats.progress * 100);
  const when = reviewWhen(card.next_review_at, now);
  return (
    // 卡片右侧留出按钮的位置（`pr-11`）：按钮在 <a> 外面（a 里嵌 button 是非法 HTML），
    // 用绝对定位压在同一角上，点卡片主体仍进详情，点按钮直接切去聊天
    <li data-testid="l-card" data-id={card.id} className="relative">
      <Link
        href={pathHref(card.id)}
        className="flex flex-col gap-2 rounded-xl border border-border bg-surface p-3 pr-11 transition-colors hover:border-primary/40"
      >
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-medium">{card.title}</h2>
            <p className="mt-0.5 truncate text-[11px] text-muted">
              {card.topic}
              {card.summary ? ` · ${card.summary}` : ""}
            </p>
          </div>
          {card.next_title && (
            <span
              data-testid="l-card-next"
              data-action={card.next_action ?? "complete"}
              className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-primary"
            >
              {t("learning.currentShort", { title: card.next_title })}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 text-[11px] text-muted">
          <span className="inline-block h-1.5 flex-1 overflow-hidden rounded-full bg-accent">
            <span
              data-testid="l-card-progress"
              data-value={percent}
              className="block h-full rounded-full bg-primary"
              style={{ width: `${percent}%` }}
            />
          </span>
          <span>{percent}%</span>
        </div>

        <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted">
          <span>{t("learning.cardMastered", { n: String(card.stats.mastered) })}</span>
          <span>{t("learning.cardTotal", { n: String(card.stats.total) })}</span>
          <span className={card.stats.weak > 0 ? "text-danger" : undefined}>
            {t("learning.statWeak", { n: String(card.stats.weak) })}
          </span>
          {/* 到期数 0 不渲染：列表里一排「到期 0」是噪声 */}
          {card.stats.due > 0 && (
            <span data-testid="l-card-due" data-value={card.stats.due} className="text-danger">
              {t("learning.statDue", { n: String(card.stats.due) })}
            </span>
          )}
          {card.next_action && card.next_action !== "complete" && (
            <span data-testid="l-card-action">{t(nextActionKey(card.next_action))}</span>
          )}
          <span className="ml-auto" data-testid="l-card-review">
            {t(when.key, when.vars)}
          </span>
        </div>
      </Link>
      <button
        type="button"
        data-testid="l-card-go"
        onClick={() => onOpen(card.id)}
        disabled={opening}
        title={t("learning.cardGoHint")}
        className="absolute right-2 top-2 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
      >
        {opening ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <MessageSquare className="h-4 w-4" />
        )}
      </button>
    </li>
  );
}

export default function LearningPage() {
  const { t, lang } = useI18n();
  const now = useNow();
  const router = useRouter();
  const { data, isLoading, error } = useLearningPaths();
  const { open, isPending } = useOpenPathChat();
  const newSession = useChatStore((state) => state.newSession);
  const setCapability = useChatStore((state) => state.setCapability);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const paths = data?.paths ?? [];

  /** 「新建学习路径」：能力强切 + 开新会话（都零 LLM）→ 跳聊天。
   *  路径本身还是聊天里的 `mastery_build` 建出来的（拍板 #3），这里只负责把用户送进去。 */
  const createPath = async () => {
    setNotice(null);
    setCreating(true);
    try {
      setCapability(CAPABILITY_MASTERY); // 先切再建：新会话按这个能力落库
      await newSession();
      router.push("/");
    } catch (err) {
      setNotice(String(err));
    } finally {
      setCreating(false);
    }
  };

  const openPath = async (id: string) => {
    setNotice(null);
    try {
      await open(id, lang);
    } catch (err) {
      setNotice(String(err));
    }
  };

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">{t("learning.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("learning.desc")}</p>
          </div>
          <button
            type="button"
            data-testid="l-new"
            onClick={() => void createPath()}
            disabled={creating}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
          >
            {creating ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            {t("learning.newPath")}
          </button>
        </div>

        {notice && <p className="text-xs text-danger">{notice}</p>}

        {/* 跨路径的「该复习了」：没有到期项时它自己整块不渲染 */}
        <DueReviewBoard />

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{String(error)}</p>
        ) : paths.length === 0 ? (
          <div
            data-testid="l-empty"
            className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center"
          >
            <GraduationCap className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("learning.empty")}</p>
            <p className="max-w-sm text-xs text-muted">{t("learning.emptyHint")}</p>
            <button
              type="button"
              data-testid="l-empty-new"
              onClick={() => void createPath()}
              disabled={creating}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
            >
              {creating ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Plus className="h-3.5 w-3.5" />
              )}
              {t("learning.newPath")}
            </button>
          </div>
        ) : (
          <ul className="space-y-2" data-testid="l-list">
            {paths.map((card) => (
              <PathCard key={card.id} card={card} now={now} onOpen={openPath} opening={isPending} />
            ))}
          </ul>
        )}
      </div>
    </main>
  );
}
