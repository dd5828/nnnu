"use client";

/** 用户消息上的引用 chips（§7.1）：来自消息 metadata 里的发送时快照。
 *
 * 已解析 → 深链跟随（`/notebooks/{nb}?record=`、`/questions?question=`），记录移动过
 * 也跟得到新位置（快照里的归属是服务端解析时的实际归属）；未解析（发送后目标被删）
 * → 灰置划线文本，不造死链。落点计算在 `lib/citations.ts:refTarget`（可单测）。 */

import Link from "next/link";
import { BookOpen, ListChecks } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { refTarget } from "@/lib/citations";
import { refKey, type RefEntry } from "@/lib/refs";

function RefIcon({ kind }: { kind: RefEntry["kind"] }) {
  return kind === "notebook_record" ? (
    <BookOpen className="h-3 w-3 shrink-0" />
  ) : (
    <ListChecks className="h-3 w-3 shrink-0" />
  );
}

export default function MessageRefs({ refs }: { refs: RefEntry[] }) {
  const { t } = useI18n();
  if (refs.length === 0) {
    return null;
  }
  return (
    <div className="mt-1 flex max-w-[85%] flex-wrap justify-end gap-1" data-testid="msg-refs">
      {refs.map((entry) => {
        const target = refTarget(entry);
        const label = target.label || t("chat.refExpired");
        if (target.kind === "internal") {
          return (
            <Link
              key={refKey(entry)}
              href={target.href}
              data-testid="msg-ref"
              title={label}
              className="inline-flex max-w-60 items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-primary/50 hover:text-foreground"
            >
              <RefIcon kind={entry.kind} />
              <span className="truncate">{label}</span>
            </Link>
          );
        }
        return (
          <span
            key={refKey(entry)}
            data-testid="msg-ref-expired"
            title={t("chat.refExpired")}
            className="inline-flex max-w-60 items-center gap-1 rounded-full border border-dashed border-border px-2 py-0.5 text-[11px] text-muted/70 line-through"
          >
            <RefIcon kind={entry.kind} />
            <span className="truncate">{label}</span>
          </span>
        );
      })}
    </div>
  );
}
