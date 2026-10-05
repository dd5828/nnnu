"use client";

/** 404（Next 约定）：应用内不存在的路径给一页说得清的收场。 */

import Link from "next/link";
import { useI18n } from "@/hooks/useI18n";

export default function NotFound() {
  const { t } = useI18n();
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-3 p-6">
      <h1 className="text-lg font-semibold">{t("common.notFoundTitle")}</h1>
      <p className="text-sm text-muted">{t("common.notFoundBody")}</p>
      <Link
        href="/"
        className="mt-1 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
      >
        {t("common.backHome")}
      </Link>
    </main>
  );
}
