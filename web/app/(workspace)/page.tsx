"use client";

/** 首页：健康卡片 + 外观快捷面板（P0 验收物）。 */

import HealthCard from "@/components/health/HealthCard";
import AppearanceQuickPanel from "@/components/settings/AppearanceQuickPanel";
import { useI18n } from "@/hooks/useI18n";

export default function HomePage() {
  const { t } = useI18n();

  return (
    <main className="flex-1 p-8">
      <h1 className="text-2xl font-semibold">{t("common.appName")}</h1>
      <p className="mt-1 text-sm text-muted">{t("common.tagline")}</p>
      <div className="mt-6 grid max-w-3xl gap-6 md:grid-cols-2">
        <HealthCard />
        <AppearanceQuickPanel />
      </div>
    </main>
  );
}
