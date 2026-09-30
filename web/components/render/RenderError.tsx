"use client";

/** 内联渲染错误卡（§7.7）：图挂了就在原地给一张说明卡，不炸整条消息。
 *
 * 刻意**不** console.error：E2E 全程挂 console 监听，页面自己知道自己画不出来就够了
 * （真出了问题服务端日志/正文里的代码还在）。
 */

import { TriangleAlert } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";

export default function RenderError({ kind, detail }: { kind: string; detail?: string }) {
  const { t } = useI18n();
  return (
    <div
      data-testid="render-error"
      className="flex flex-col gap-1 rounded-xl border border-border bg-surface px-3 py-2.5 text-xs text-muted"
    >
      <div className="flex items-center gap-1.5 text-danger">
        <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
        {t("render.error", { kind })}
      </div>
      {detail && <div className="break-all text-[11px] opacity-80">{detail}</div>}
    </div>
  );
}
