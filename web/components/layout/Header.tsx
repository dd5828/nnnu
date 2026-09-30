"use client";

/** 页头：窄屏汉堡（开侧栏抽屉）+ 占位标题 + 后端健康状态。 */

import { Menu } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import HealthDot from "@/components/health/HealthDot";

export default function Header({ onMenu }: { onMenu?: () => void }) {
  const { t } = useI18n();
  return (
    <header className="flex h-12 items-center justify-between border-b border-border px-6">
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          data-testid="nav-menu"
          onClick={onMenu}
          className="-ml-2 shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground md:hidden"
          aria-label={t("common.menu")}
          title={t("common.menu")}
        >
          <Menu className="h-4 w-4" />
        </button>
        <span className="truncate text-sm text-muted">{t("common.tagline")}</span>
      </div>
      <HealthDot />
    </header>
  );
}
