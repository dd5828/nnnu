"use client";

/** 空态欢迎页（新会话时展示）。 */

import { useI18n } from "@/hooks/useI18n";

export default function EmptyState() {
  const { t } = useI18n();
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center">
      <div className="max-w-md px-6 text-center">
        <h1 className="text-2xl font-semibold">{t("common.appName")}</h1>
        <p className="mt-1 text-sm text-muted">{t("common.tagline")}</p>
        <p className="mt-6 text-sm text-muted">{t("chat.emptyHint")}</p>
      </div>
    </div>
  );
}
