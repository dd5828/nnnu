"use client";

/**
 * 预览器共用的加载/失败态。
 *
 * PreviewSpinner 无 props，同时当 next/dynamic 的 loading 占位和各渲染器
 * 取数中的占位——两处长得一样，没必要写两份。
 */

import { AlertCircle, Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";

export function PreviewSpinner() {
  const { t } = useI18n();
  return (
    <div className="flex h-full items-center justify-center gap-2 text-xs text-muted">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span>{t("knowledge.readerLoading")}</span>
    </div>
  );
}

/** 取数失败的统一样子：太大会劝下载，其余归为加载失败（头部下载按钮是兜底）。 */
export function PreviewFailure({ code }: { code: "too_large" | "load" }) {
  const { t } = useI18n();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center text-xs text-muted">
      <AlertCircle className="h-4 w-4 opacity-70" />
      <p>{code === "too_large" ? t("knowledge.readerTooLarge") : t("knowledge.readerLoadError")}</p>
    </div>
  );
}
