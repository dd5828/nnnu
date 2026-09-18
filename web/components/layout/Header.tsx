"use client";

/** 页头：占位标题 + 后端健康状态。 */

import { useI18n } from "@/hooks/useI18n";
import HealthDot from "@/components/health/HealthDot";

export default function Header() {
  const { t } = useI18n();
  return (
    <header className="flex h-12 items-center justify-between border-b border-border px-6">
      <span className="text-sm text-muted">{t("common.tagline")}</span>
      <HealthDot />
    </header>
  );
}
