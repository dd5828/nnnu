"use client";

/** 记忆工作台的选中项芯片行（L3 文档 / L2·L1 分面 / 文件，共用一套外观）。 */

export interface ChipItem {
  value: string;
  label: string;
  count?: number;
}

interface Props {
  items: ChipItem[];
  active: string | null;
  onSelect: (value: string) => void;
  testId?: string;
}

export default function ChipRow({ items, active, onSelect, testId }: Props) {
  return (
    <div data-testid={testId} className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          data-value={item.value}
          aria-pressed={item.value === active}
          onClick={() => onSelect(item.value)}
          className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors ${
            item.value === active
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-surface hover:bg-accent"
          }`}
        >
          {item.label}
          {item.count !== undefined && <span className="opacity-70">{item.count}</span>}
        </button>
      ))}
    </div>
  );
}
