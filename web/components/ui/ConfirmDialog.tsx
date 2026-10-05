"use client";

/** 确认弹窗宿主：全局唯一一份，由 useConfirm 的 confirmDialog() 驱动。 */

import { useEffect, useRef } from "react";
import { useConfirmStore } from "@/hooks/useConfirm";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useI18n } from "@/hooks/useI18n";

export default function ConfirmDialog() {
  const { t } = useI18n();
  const pending = useConfirmStore((s) => s.pending);
  const settle = useConfirmStore((s) => s.settle);
  const dialogRef = useRef<HTMLDivElement>(null);
  // 打开期间焦点关进弹窗；关闭时 useFocusTrap 自动还给触发按钮
  useFocusTrap(dialogRef, pending !== null);

  useEffect(() => {
    if (!pending) {
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        settle(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, settle]);

  if (!pending) {
    return null;
  }
  const title = pending.title ?? "";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        data-testid="confirm-overlay"
        className="absolute inset-0 bg-black/40"
        onClick={() => settle(false)}
        aria-hidden
      />
      <div
        ref={dialogRef}
        data-testid="confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-label={title || pending.message}
        className="relative w-full max-w-sm rounded-xl border border-border bg-surface p-5 shadow-lg"
      >
        {title && <h2 className="text-sm font-semibold">{title}</h2>}
        <p className="mt-1.5 whitespace-pre-wrap text-sm text-muted">{pending.message}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            data-testid="confirm-cancel"
            onClick={() => settle(false)}
            className="rounded-lg border border-border px-3.5 py-1.5 text-sm transition-colors hover:bg-accent"
          >
            {pending.cancelLabel ?? t("common.cancel")}
          </button>
          <button
            type="button"
            data-testid="confirm-accept"
            onClick={() => settle(true)}
            className={`rounded-lg px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 ${
              pending.danger ? "bg-danger" : "bg-primary"
            }`}
          >
            {pending.confirmLabel ?? t("common.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}
