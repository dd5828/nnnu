"use client";

/** 设置中心（§7.19）：卡片化表单（spec 驱动）+ 草稿-应用 + 模型探测 + 健康条。 */

import HealthCard from "@/components/health/HealthCard";
import SettingsForm from "@/components/settings/SettingsForm";
import ToolCatalog from "@/components/settings/ToolCatalog";
import { useI18n } from "@/hooks/useI18n";

export default function SettingsPage() {
  const { t } = useI18n();
  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-6">
        <h1 className="text-xl font-semibold">{t("nav.settings")}</h1>
        <SettingsForm
          area="appearance"
          title={t("settings.appearanceTitle")}
          description={t("settings.appearanceDesc")}
        />
        <SettingsForm
          area="models"
          title={t("settings.modelsTitle")}
          description={t("settings.modelsDesc")}
        />
        <SettingsForm area="kb" title={t("settings.kbTitle")} description={t("settings.kbDesc")} />
        <SettingsForm
          area="chat"
          title={t("settings.chatTitle")}
          description={t("settings.chatDesc")}
        />
        <ToolCatalog />
        <SettingsForm area="network" title={t("settings.networkTitle")} />
        <SettingsForm area="system" title={t("settings.systemTitle")} />
        <HealthCard />
      </div>
    </main>
  );
}
