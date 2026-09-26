/** 学习看板的纯计算（§7.5）：圆环角度、缩进、深链、文案键。
 *
 * 全部是纯函数，交给 `tests/learning.test.ts` 盯（组件测试在本仓跑不起来——
 * vitest 是 node 环境，只收 tests 目录下的 .test.ts 文件）。
 *
 * **门不再镜像**：`gate`/`gate_kind`/`cleared`/`due` 都是服务端的 `@computed_field`，
 * 跟着节点响应一起来（见 `services/learning/models.py`）。前端只读，不自己算门——
 * 上一版在这儿抄了一份 80/85/90 的表，改口径时两边一起飘。
 */

import type { LearningNode, NodeState, NodeType } from "@/types/api";

export const NODE_TYPES: NodeType[] = ["memory", "procedure", "concept", "design"];

/** 掌握度（0–100）→ 圆环扫过的角度；越界夹到 0–360，坏数据不画出鬼环。 */
export function sweepDegrees(mastery: number): number {
  if (!Number.isFinite(mastery)) {
    return 0;
  }
  return Math.min(360, Math.max(0, mastery) * 3.6);
}

/** 是否过门：**读服务端给的 `cleared`**（定性节点没有数字门，本地比不了）。 */
export function clearedOf(node: Pick<LearningNode, "cleared">): boolean {
  return Boolean(node.cleared);
}

/** 圆环颜色走主题 token（硬编码 hex 会破四套主题）：
 * 已过门 → 绿（**先判 cleared**：到期待复习的节点仍然是学会了，不能画成红的）；
 * 没过门：未开始灰、在学蓝、待复习红。
 */
export function ringTone(cleared: boolean, state: NodeState): string {
  if (cleared) {
    return "text-success";
  }
  if (state === "reviewing") {
    return "text-danger";
  }
  return state === "not_started" ? "text-muted" : "text-primary";
}

/** 树的缩进：每深一层 16px（父带子靠深度，不靠嵌套 DOM）。 */
export function indentPx(depth: number): number {
  return Math.max(0, depth) * 16;
}

/** 薄弱点深链到题库页：只看这个节点的题（题库页读 ?node=）。 */
export function weakPointHref(nodeId: string): string {
  return `/questions?node=${encodeURIComponent(nodeId)}`;
}

/** 路径详情页地址（卡片点进去）。 */
export function pathHref(pathId: string): string {
  return `/learning/${encodeURIComponent(pathId)}`;
}

/** 节点详情页锚（同页切换选中的节点，走 query 好分享）。 */
export function nodeHref(pathId: string, nodeId: string): string {
  return `${pathHref(pathId)}?node=${encodeURIComponent(nodeId)}`;
}

export function nodeTypeKey(nodeType: string): string {
  return `learning.type_${nodeType}`;
}

export function nodeStateKey(state: string): string {
  return `learning.state_${state}`;
}

/** 教学策略键：指向 locales 里的一句人话（后端提示词的 strategies.* 是给模型看的）。 */
export function strategyKey(nodeType: string): string {
  return `learning.strategy_${nodeType}`;
}

/** 下一目标建议动作的文案键（`NextTarget.action`，见后端 NEXT_ACTIONS）。 */
export function nextActionKey(action: string | null | undefined): string {
  return `learning.action_${action || "complete"}`;
}

/** 门的一句话文案：定量给分数线，定性说「讲一遍过关」。 */
export function gateKey(node: Pick<LearningNode, "gate" | "gate_kind">): {
  key: string;
  vars: Record<string, string>;
} {
  if (node.gate_kind === "quantitative" && node.gate !== null) {
    return { key: "learning.gateQuantitative", vars: { gate: String(Math.round(node.gate)) } };
  }
  return { key: "learning.gateQualitative", vars: {} };
}

/** 复习时间的展示文案：到期/逾期 → 立刻，未来 → 还剩几天。 */
export function reviewWhen(
  nextReviewAt: number | null,
  now: number
): { key: string; vars: Record<string, string> } {
  if (nextReviewAt === null) {
    return { key: "learning.reviewNone", vars: {} };
  }
  const days = (nextReviewAt - now) / 86400;
  if (days <= 0) {
    return { key: "learning.reviewDue", vars: {} };
  }
  return { key: "learning.reviewInDays", vars: { n: String(Math.max(1, Math.ceil(days))) } };
}

/** 节点进度条/仪表用的百分比整数（节点级掌握度已经是 0–100）。 */
export function masteryPercent(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.round(Math.min(100, Math.max(0, value)));
}
