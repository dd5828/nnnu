"use client";

/**
 * 检索引擎选择器（§7.9 P14）：建库时选，详情页换。
 *
 * 没装可选依赖的引擎照样列出来（与后端 registry 一致）——藏掉入口，用户只会
 * 以为功能不存在。选中不可用引擎时把安装/配置说明原样摆在下面。
 */

import { useEngines } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";

interface Props {
  value: string;
  onChange: (engine: string) => void;
  disabled?: boolean;
  compact?: boolean;
  testId?: string;
}

export default function EnginePicker({
  value,
  onChange,
  disabled = false,
  compact = false,
  testId,
}: Props) {
  const { t } = useI18n();
  const { data, isLoading } = useEngines();
  const engines = data?.engines ?? [];
  const selected = engines.find((engine) => engine.id === value);

  return (
    <div>
      <div className={compact ? "flex items-center gap-2" : "mb-1 flex items-baseline gap-2"}>
        <label htmlFor={testId ?? "kb-engine"} className="text-sm font-medium">
          {t("knowledge.engineLabel")}
        </label>
        {!compact && <span className="text-xs text-muted">{t("knowledge.engineHint")}</span>}
        <select
          id={testId ?? "kb-engine"}
          data-testid={testId}
          value={value}
          disabled={disabled || isLoading || engines.length === 0}
          onChange={(event) => onChange(event.target.value)}
          className="rounded-lg border border-border bg-background/40 px-2 py-1 text-sm outline-none transition-colors focus:border-primary/60 disabled:opacity-60"
        >
          {engines.map((engine) => (
            <option key={engine.id} value={engine.id} disabled={!engine.available}>
              {t(`knowledge.engine_${engine.id}`)}
              {engine.available ? "" : ` · ${t("knowledge.engineUnavailable")}`}
            </option>
          ))}
        </select>
      </div>
      {selected && (!selected.available || !compact) && (
        <p className={`text-xs ${selected.available ? "text-muted" : "text-danger"}`}>
          {selected.available
            ? t(`knowledge.engineHint_${selected.id}`)
            : selected.reason || t("knowledge.engineUnavailable")}
        </p>
      )}
    </div>
  );
}
