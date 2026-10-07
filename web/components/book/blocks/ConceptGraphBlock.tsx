"use client";

/** 概念图块：确定性布局 + SVG 直绘（照 MemoryGraph 手法：颜色只表达分组，
 *  分组名同时出图例与节点 <title>，不靠颜色单独传达信息）。 */

import { useMemo } from "react";
import { useI18n } from "@/hooks/useI18n";
import {
  conceptGroups,
  edgeLabel,
  groupFill,
  layoutConceptGraph,
  parseConceptGraph,
} from "@/lib/book-graph";
import type { BookBlock } from "@/types/api";

export default function ConceptGraphBlock({ block }: { block: BookBlock }) {
  const { t } = useI18n();
  const graph = useMemo(() => parseConceptGraph(block.payload), [block.payload]);
  const groups = useMemo(() => conceptGroups(graph.nodes), [graph.nodes]);
  const layout = useMemo(() => layoutConceptGraph(graph), [graph]);
  // 布局丢边/丢点与解析口径一致（解析时就滤过自环与悬空端点），下标对得上
  const metaById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes]
  );

  if (graph.nodes.length === 0) {
    return <p className="text-xs text-muted">{t("book.readerEmpty")}</p>;
  }

  return (
    <div data-testid="bk-graph" data-block-id={block.id}>
      {groups.length > 1 && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
          {groups.map((group) => (
            <span key={group} className="inline-flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block size-2.5 rounded-full"
                style={{ background: groupFill(group, groups) }}
              />
              {group}
            </span>
          ))}
        </div>
      )}
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="img"
        aria-label={block.title || t("book.blockType_concept_graph")}
        className="h-[380px] w-full select-none"
      >
        <g>
          {layout.edges.map((edge, index) => {
            const label = graph.edges[index]?.label ?? "";
            const midX = (edge.x1 + edge.x2) / 2;
            const midY = (edge.y1 + edge.y2) / 2;
            return (
              <g key={`edge-${index}`}>
                <line
                  x1={edge.x1}
                  y1={edge.y1}
                  x2={edge.x2}
                  y2={edge.y2}
                  style={{ stroke: "var(--border)" }}
                  strokeWidth={1.5}
                />
                {label && (
                  <>
                    <rect
                      x={midX - 28}
                      y={midY - 8}
                      width={56}
                      height={14}
                      rx={7}
                      style={{ fill: "var(--surface)" }}
                    />
                    <text
                      x={midX}
                      y={midY + 2}
                      textAnchor="middle"
                      fontSize={9}
                      style={{ fill: "var(--muted)" }}
                    >
                      {edgeLabel(label)}
                      <title>{label}</title>
                    </text>
                  </>
                )}
              </g>
            );
          })}
        </g>
        {layout.nodes.map((node) => (
          <g key={node.id} data-testid="bk-graph-node" data-id={node.id}>
            <title>{metaById.get(node.id)?.label ?? node.label}</title>
            <circle
              cx={node.x}
              cy={node.y}
              r={node.r}
              style={{
                fill: groupFill(metaById.get(node.id)?.group ?? "", groups),
                stroke: "var(--surface)",
              }}
              strokeWidth={2}
            />
            <text
              x={node.x}
              y={node.y + node.r + 13}
              textAnchor="middle"
              fontSize={11}
              style={{ fill: "var(--foreground)" }}
            >
              {node.label}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}
