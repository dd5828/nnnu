"use client";

/** 输入区工具栏的小下拉（从 ResearchSettings 里提出来复用）：
 *  研究档位/模式、可视化渲染类型、动画画质四处长的都是这一个形状。
 *
 * 只管长相与开合：`open`/`onToggle` 由调用方持有（一个回合里同时只允许开一个时，
 * 状态得放在共同的父组件里），点外面关掉的逻辑也归调用方的根节点。
 */

import { Check, ChevronDown } from "lucide-react";

export function ToolbarDropdown({
  testId,
  value,
  label,
  title,
  disabled,
  open,
  onToggle,
  children,
}: {
  testId: string;
  value: string;
  label: string;
  title: string;
  disabled: boolean;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="relative">
      <button
        type="button"
        data-testid={testId}
        data-value={value}
        onClick={onToggle}
        disabled={disabled}
        aria-expanded={open}
        title={title}
        className="flex items-center gap-1 rounded-lg px-2 py-2 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className="truncate">{label}</span>
        <ChevronDown className="h-3 w-3 shrink-0" />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(260px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface py-1 shadow-lg">
          {children}
        </div>
      )}
    </div>
  );
}

export function ToolbarOption({
  testId,
  value,
  selected,
  label,
  description,
  onPick,
}: {
  testId: string;
  value: string;
  selected: boolean;
  label: string;
  description: string;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      data-value={value}
      onClick={onPick}
      className={`flex w-full items-start gap-2.5 px-3 py-2 text-left text-xs transition-colors ${
        selected ? "bg-primary/5" : "hover:bg-accent"
      }`}
    >
      <span className="min-w-0 flex-1">
        <span className="block font-medium">{label}</span>
        <span className="mt-0.5 block text-[11px] text-muted">{description}</span>
      </span>
      {selected && <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />}
    </button>
  );
}
