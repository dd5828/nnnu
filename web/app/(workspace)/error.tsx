"use client";

/** 工作台错误边界（Next 约定）：某个页面渲染/取数抛出时只牺牲主区，侧栏与页头还在。 */

import Link from "next/link";
import { RotateCcw } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";

export default function WorkspaceError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const { t } = useI18n();
  return (
    <main className="flex min-h-0 flex-1 items-center justify-center p-6">
      <div className="w-full max-w-md rounded-xl border border-border bg-surface p-6 text-center">
        <h1 className="text-base font-semibold">{t("common.errorTitle")}</h1>
        <p className="mt-1.5 text-sm text-muted">{t("common.errorBody")}</p>
        <div className="mt-4 flex items-center justify-center gap-2">
          <button
            type="button"
            onClick={reset}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {t("common.retry")}
          </button>
          <Link
            href="/"
            className="rounded-lg border border-border px-3.5 py-1.5 text-sm transition-colors hover:bg-accent"
          >
            {t("common.backHome")}
          </Link>
        </div>
      </div>
    </main>
  );
}
