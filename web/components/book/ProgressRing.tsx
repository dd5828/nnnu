"use client";

/** 阅读进度环（书库卡片与控制台共用）：纯 SVG，进度 0~100 夹紧。
 *
 * 颜色走 currentColor（text-accent / text-primary 两个类），不引新的 stroke 令牌。
 */

export default function ProgressRing({
  percent,
  size = 44,
  label,
}: {
  percent: number;
  size?: number;
  label?: string;
}) {
  const clamped = Math.max(0, Math.min(100, Math.round(Number.isFinite(percent) ? percent : 0)));
  const stroke = 4;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const dash = (clamped / 100) * circumference;

  return (
    <div
      className="relative shrink-0"
      style={{ width: size, height: size }}
      role="img"
      aria-label={label}
      data-testid="bk-ring"
      data-percent={clamped}
    >
      <svg width={size} height={size} className="-rotate-90 text-accent">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
          className="text-primary transition-all"
        />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-[10px] text-muted">
        {clamped}%
      </span>
    </div>
  );
}
