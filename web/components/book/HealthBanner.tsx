"use client";

/** 源漂移横幅（ready 且拍过快照的书）：编译后素材变过就提示，重编按章来。
 *
 * 漂移条目是「源」级别的（文档/笔记/题目），哪一章受影响靠章的 source_refs 反查
 * （lib/book.ts 的 driftChapters）——查不到就只提示「整本重编」。
 */

import { AlertTriangle, RotateCcw } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { driftChapters } from "@/lib/book";
import type { BookHealth, BookSpine } from "@/types/api";

const CHANGE_KEY = {
  added: "book.healthChange_added",
  changed: "book.healthChange_changed",
  missing: "book.healthChange_missing",
} as const;

const CHANGE_CLASS = {
  added: "border-success/40 text-success",
  changed: "border-primary/40 text-primary",
  missing: "border-danger/40 text-danger",
} as const;

interface Props {
  spine: BookSpine;
  health: BookHealth | null;
  busy: boolean;
  onRecompileChapter: (chapterKey: string) => void;
}

export default function HealthBanner({ spine, health, busy, onRecompileChapter }: Props) {
  const { t } = useI18n();
  if (!health || health.status !== "drift" || health.drift.length === 0) {
    return null;
  }
  const affected = driftChapters(spine, health.drift);

  return (
    <div data-testid="bk-health" className="rounded-xl border border-danger/40 bg-surface p-4">
      <div className="flex items-center gap-2">
        <AlertTriangle className="h-4 w-4 shrink-0 text-danger" />
        <span className="text-sm font-medium">{t("book.healthTitle")}</span>
        <span className="text-xs text-muted">
          {t("book.healthCounts", {
            added: String(health.counts.added),
            changed: String(health.counts.changed),
            missing: String(health.counts.missing),
          })}
        </span>
      </div>

      <ul className="mt-2 space-y-1">
        {health.drift.slice(0, 12).map((item, index) => (
          <li
            key={`${item.change}-${item.ref}-${index}`}
            data-testid="bk-drift-item"
            data-change={item.change}
            className="flex items-center gap-2 text-xs"
          >
            <span
              className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] ${
                CHANGE_CLASS[item.change]
              }`}
            >
              {t(CHANGE_KEY[item.change])}
            </span>
            <span className="min-w-0 flex-1 truncate text-muted">{item.label || item.ref}</span>
          </li>
        ))}
      </ul>

      <p className="mt-2 text-xs text-muted">{t("book.healthHint")}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {affected.map((chapter) => (
          <button
            key={chapter.key}
            type="button"
            data-testid="bk-health-recompile"
            data-key={chapter.key}
            disabled={busy}
            onClick={() => onRecompileChapter(chapter.key)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
          >
            <RotateCcw className="h-3 w-3" />
            {t("book.recompileNamed", { title: chapter.title })}
          </button>
        ))}
        {affected.length === 0 && (
          <span className="text-xs text-muted">{t("book.healthNoChapter")}</span>
        )}
      </div>
    </div>
  );
}
