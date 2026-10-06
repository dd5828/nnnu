"use client";

/** 输入区的待发引用 chips（§7.1）：「+」菜单选中的条目摆在这儿，发送前可逐个撤掉。
 *  发送动作在 store 的 send() 里统一清空（一次性语义），这里只管展示与移除。 */

import { BookOpen, ListChecks, X } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { refKey } from "@/lib/refs";

export default function RefChips() {
  const { t } = useI18n();
  const pendingRefs = useChatStore((s) => s.pendingRefs);
  const removePendingRef = useChatStore((s) => s.removePendingRef);

  if (pendingRefs.length === 0) {
    return null;
  }
  return (
    <>
      {pendingRefs.map((ref) => (
        <span
          key={refKey(ref)}
          data-testid="ref-chip"
          data-kind={ref.kind}
          className="inline-flex max-w-60 items-center gap-1 rounded-full border border-primary/40 bg-primary/10 px-2.5 py-1 text-xs"
        >
          {ref.kind === "notebook_record" ? (
            <BookOpen className="h-3 w-3 shrink-0 text-primary" />
          ) : (
            <ListChecks className="h-3 w-3 shrink-0 text-primary" />
          )}
          <span className="truncate">{ref.label}</span>
          <button
            type="button"
            data-testid="ref-chip-remove"
            onClick={() => removePendingRef(ref)}
            className="text-muted transition-colors hover:text-danger"
            aria-label={t("chat.refRemove")}
          >
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}
    </>
  );
}
