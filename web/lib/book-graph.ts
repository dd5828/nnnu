/** 活书概念图（§7.14 concept_graph 块）：{nodes, edges} → 确定性力导向布局 + 分组配色。
 *
 * 复用 memory-graph 的 `layoutGraph`（照 semantic-graph 先例）：概念图与语义图都是
 * 「节点 + 无向摆位 + 有向语义」，一套确定性 FR 就够，不引第二套物理参数；渲染走
 * SVG 直绘（照 MemoryGraph 先例，颜色用主题 CSS 变量，四个主题自动跟随）。
 *
 * 分组配色按**首现序**从既有分类色轮转（dataviz：分类色固定序，图例与节点同色
 * 双通道，不靠颜色单独传达信息）。
 */

import { layoutGraph, type GraphLayout } from "@/lib/memory-graph";
import type { MemoryGraphNode } from "@/types/api";

export interface ConceptNode {
  id: string;
  label: string;
  group: string;
}

export interface ConceptEdge {
  source: string;
  target: string;
  label: string;
}

export interface ConceptGraph {
  nodes: ConceptNode[];
  edges: ConceptEdge[];
}

/** 分组色轮（globals.css 里经 dataviz 校验的分类色，四主题各有一组）。 */
export const GROUP_FILL_TOKENS: readonly string[] = [
  "var(--graph-ent-topic)",
  "var(--graph-ent-person)",
  "var(--graph-ent-project)",
  "var(--graph-ent-preference)",
  "var(--graph-ent-other)",
];

/** 分组列表（按首现序去重；配色下标即它）。 */
export function conceptGroups(nodes: ConceptNode[]): string[] {
  const groups: string[] = [];
  for (const node of nodes) {
    if (node.group && !groups.includes(node.group)) {
      groups.push(node.group);
    }
  }
  return groups;
}

/** 分组颜色：下标轮转色轮（分组多于色数就循环，图例文字才是主通道）。 */
export function groupFill(group: string, groups: string[]): string {
  const index = Math.max(0, groups.indexOf(group));
  return GROUP_FILL_TOKENS[index % GROUP_FILL_TOKENS.length];
}

/** 宽容解析块 payload（形状已由后端校验，这里防旧数据/手改 JSON 弄崩阅读器）。 */
export function parseConceptGraph(payload: Record<string, unknown>): ConceptGraph {
  const rawNodes = Array.isArray(payload.nodes) ? payload.nodes : [];
  const nodes = rawNodes.flatMap((item) => {
    if (typeof item !== "object" || item === null) {
      return [];
    }
    const record = item as Record<string, unknown>;
    const id = typeof record.id === "string" ? record.id : "";
    const label = typeof record.label === "string" ? record.label : "";
    const group = typeof record.group === "string" ? record.group : "";
    return id && label ? [{ id, label, group }] : [];
  });
  const ids = new Set(nodes.map((node) => node.id));
  const rawEdges = Array.isArray(payload.edges) ? payload.edges : [];
  const edges = rawEdges.flatMap((item) => {
    if (typeof item !== "object" || item === null) {
      return [];
    }
    const record = item as Record<string, unknown>;
    const source = typeof record.source === "string" ? record.source : "";
    const target = typeof record.target === "string" ? record.target : "";
    const label = typeof record.label === "string" ? record.label : "";
    // 悬空端点与自环在解析时就丢：layoutGraph 同样会丢，先丢好让「布局边 ↔ 边标签」
    // 的下标一一对上（渲染层按下标取标签）
    return source && target && source !== target && ids.has(source) && ids.has(target)
      ? [{ source, target, label }]
      : [];
  });
  return { nodes, edges };
}

interface Options {
  width?: number;
  height?: number;
  labelLimit?: number;
}

/** 概念图布局：节点映射成伪记忆节点（借 l2 的 12px 半径档位），边直传 layoutGraph。 */
export function layoutConceptGraph(graph: ConceptGraph, options: Options = {}): GraphLayout {
  const pseudo: MemoryGraphNode[] = graph.nodes.map((node) => ({
    id: node.id,
    layer: "l2",
    key: "",
    kind: "entry",
    text: node.label,
    date: "",
    ref: "",
    stale: false,
    edited: false,
    origin: "consolidator",
    broken: false,
  }));
  return layoutGraph(
    pseudo,
    graph.edges.map((edge) => ({ source: edge.source, target: edge.target })),
    { labelLimit: 14, ...options }
  );
}

/** 边标签（画在连线中点；太长截断，完整内容在 <title> 里）。 */
export function edgeLabel(text: string, limit = 10): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}
