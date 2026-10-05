"use client";

/** 回合成本摘要（§6.9 cost_summary）。 */

import { Coins } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { formatNumber } from "@/lib/format";

export default function CostBadge({ tokens, cost }: { tokens: number; cost: number }) {
  const { t, lang } = useI18n();
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-accent/60 px-2 py-0.5 text-[11px] text-muted">
      <Coins className="h-3 w-3" />
      {t("chat.cost")}: {formatNumber(tokens, lang)} tokens · ${cost.toFixed(6)}
    </span>
  );
}
