"use client";

/** 改写 diff 弹层（§7.13）：确认前文档不变，accept 才写回。
 *
 * 只负责展示与两个出口；写回编排（先 cancel 防抖再本地替换）在工作台页。
 * Esc / 点遮罩 = reject——和 ConfirmDialog 同款习惯：关掉弹层不该悄悄落改动。
 */

import { useEffect, useRef } from "react";
import { Loader2 } from "lucide-react";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useI18n } from "@/hooks/useI18n";
import type { CoWriterEditResult } from "@/types/api";
import EditTracePanel from "./EditTracePanel";

/** 行级 diff 的行样式：增行绿底、删行红底、正文原样。 */
const TAG_CLASS: Record<string, string> = {
  add: "bg-success/10 text-success",
  del: "bg-danger/10 text-danger line-through",
  eq: "",
};

const TAG_PREFIX: Record<string, string> = { add: "+ ", del: "- ", eq: "  " };

export default function EditDiffDialog({
  result,
  busy,
  error,
  onAccept,
  onReject,
}: {
  result: CoWriterEditResult;
  /** accept/reject 请求进行中：按钮禁用，Esc 也不放行。 */
  busy: boolean;
  /** 失败原因（doc_changed/edit_expired 等），展示在按钮上方。 */
  error: string | null;
  onAccept: () => void;
  onReject: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef, true);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        onReject();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onReject]);

  const { stats, ops } = result;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/40"
        onClick={() => !busy && onReject()}
        aria-hidden
      />
      <div
        ref={dialogRef}
        data-testid="cw-diff"
        role="alertdialog"
        aria-modal="true"
        aria-label={t("coWriter.diffTitle")}
        className="relative flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
      >
        <div className="flex items-baseline gap-3 border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">{t("coWriter.diffTitle")}</h2>
          <span className="text-xs text-muted">
            {t("coWriter.diffStats", {
              added: String(stats.added),
              deleted: String(stats.deleted),
              kept: String(stats.kept),
            })}
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto font-mono text-xs leading-relaxed">
          {ops.map((op, index) => (
            <div
              key={index}
              data-tag={op.tag}
              className={`whitespace-pre-wrap break-all px-4 py-0.5 ${TAG_CLASS[op.tag]}`}
            >
              {TAG_PREFIX[op.tag]}
              {op.text.replace(/\n$/, "")}
            </div>
          ))}
        </div>

        <EditTracePanel
          trace={result.trace}
          citations={result.citations}
          degraded={result.degraded}
        />

        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          {error && <span className="mr-auto text-xs text-danger">{error}</span>}
          <button
            type="button"
            data-testid="cw-reject"
            disabled={busy}
            onClick={onReject}
            className="rounded-lg border border-border px-3.5 py-1.5 text-sm transition-colors hover:bg-accent disabled:opacity-50"
          >
            {t("coWriter.reject")}
          </button>
          <button
            type="button"
            data-testid="cw-accept"
            disabled={busy}
            onClick={onAccept}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {t("coWriter.accept")}
          </button>
        </div>
      </div>
    </div>
  );
}
