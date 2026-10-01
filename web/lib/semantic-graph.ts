/** 语义知识图谱布局（§7.10 补做）：实体/关系映射到既有力导向布局。
 *
 * 为什么复用 `layoutGraph` 不另写：证据链和语义图都是「节点 + 无向摆位 + 有向语义」，
 * 布局规则（确定性、越界夹取、收尾取景）一套就够；语义侧只是把实体名当节点 id、
 * 借 l2 的半径档位（12px），不引入第二套物理参数。
 *
 * 颜色只表达实体类型（dataviz：分类色按固定序、粉↔青绿 CVD ΔE 在地板带，故必须
 * 配文字二级编码——图例、实体行类型标签、连线 tooltip 三级都在，不许删）。
 */

import { layoutGraph, type GraphLayout } from "@/lib/memory-graph";
import type { MemoryGraphNode, SemanticGraphEdge, SemanticGraphNode } from "@/types/api";

export const ENTITY_TYPES = ["person", "topic", "project", "preference", "other"] as const;

/** 实体类型 → 颜色 token（四主题各自定义，见 globals.css）。 */
export const ENTITY_FILL: Record<string, string> = {
  person: "var(--graph-ent-person)",
  topic: "var(--graph-ent-topic)",
  project: "var(--graph-ent-project)",
  preference: "var(--graph-ent-preference)",
  other: "var(--graph-ent-other)",
};

const ENTITY_LABEL_KEYS: Record<string, string> = {
  person: "memory.entityPerson",
  topic: "memory.entityTopic",
  project: "memory.entityProject",
  preference: "memory.entityPreference",
  other: "memory.entityOther",
};

/** 未知类型（旧工件手写）→ other 的文案与颜色，不吞不炸。 */
export function entityTypeLabelKey(type: string): string {
  return ENTITY_LABEL_KEYS[type] ?? ENTITY_LABEL_KEYS.other;
}

export function entityFill(type: string): string {
  return ENTITY_FILL[type] ?? ENTITY_FILL.other;
}

interface Options {
  width?: number;
  height?: number;
  labelLimit?: number;
}

/** 语义图布局：实体名即节点 id（后端已按名归并），关系边直传 layoutGraph。 */
export function layoutSemanticGraph(
  nodes: SemanticGraphNode[],
  edges: SemanticGraphEdge[],
  options: Options = {}
): GraphLayout {
  const pseudo: MemoryGraphNode[] = nodes.map((node) => ({
    id: node.name,
    layer: "l2",
    key: "",
    kind: "entry",
    text: node.name,
    date: "",
    ref: "",
    stale: false,
    edited: false,
    origin: "consolidator",
    broken: false,
  }));
  return layoutGraph(
    pseudo,
    edges.map((edge) => ({ source: edge.source, target: edge.target })),
    options
  );
}
