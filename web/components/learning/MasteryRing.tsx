"use client";

/** 掌握度圆环（看板仪表）：纯 CSS conic-gradient，颜色走 `currentColor` + 主题 token。
 *
 * 定量节点：外圈是掌握度（从 12 点顺时针扫 `sweepDegrees`），环上那道短刻度是
 * **门**——离门槛还差多少要一眼看得见，不然 89 分和 0 分看着一样。
 * 定性节点（`gate === null`）：没有分数线可画，整环只用颜色表达「已评定 / 待评定」，
 * 中间显示的是展示分（过了 100，没过封顶 40）。
 *
 * 门与是否过门都从 payload 读（`gate` / `gate_kind` / `cleared`），前端不再镜像一份表。
 */

import { masteryPercent, ringTone, sweepDegrees } from "@/lib/learning";
import type { GateKind, NodeState } from "@/types/api";

export default function MasteryRing({
  mastery,
  state,
  gate,
  gateKind,
  cleared,
  size = 72,
}: {
  mastery: number;
  state: NodeState;
  gate: number | null;
  gateKind: GateKind;
  cleared: boolean;
  size?: number;
}) {
  const sweep = sweepDegrees(cleared && gate === null ? 100 : mastery);
  const gateAngle = gate === null ? 0 : sweepDegrees(gate);

  return (
    <div
      data-testid="l-mastery"
      data-value={masteryPercent(mastery)}
      data-state={state}
      data-gate={gate ?? ""}
      data-gate-kind={gateKind}
      data-cleared={cleared ? "true" : "false"}
      className={`relative shrink-0 ${ringTone(cleared, state)}`}
      style={{ width: size, height: size }}
      title={
        gate === null
          ? `${masteryPercent(mastery)}（定性门）`
          : `${masteryPercent(mastery)} / ${gate}`
      }
    >
      {/* 底环（accent 是主题 token，不是灰） */}
      <div className="absolute inset-0 rounded-full bg-accent" />
      {/* 掌握度：扫过的角度用 currentColor，剩下部分透明，露出底环 */}
      <div
        className="absolute inset-0 rounded-full"
        style={{ background: `conic-gradient(currentColor ${sweep}deg, transparent 0deg)` }}
      />
      {/* 门刻度：整层绕中心转到 gate 角度，条子固定在 12 点（定性节点不画） */}
      {gate !== null ? (
        <div className="absolute inset-0" style={{ transform: `rotate(${gateAngle}deg)` }}>
          <span
            data-testid="l-gate"
            data-value={gate}
            className="absolute left-1/2 top-0 h-2 w-[3px] -translate-x-1/2 rounded-full bg-foreground/70"
          />
        </div>
      ) : null}
      {/* 挖空中心 → 变成环 */}
      <div className="absolute inset-[7px] flex items-center justify-center rounded-full bg-surface">
        <span className="text-sm font-semibold text-foreground">{masteryPercent(mastery)}</span>
      </div>
    </div>
  );
}
