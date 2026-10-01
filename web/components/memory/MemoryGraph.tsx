"use client";

/**
 * Memory Graph（§7.10 验收②）：L3 图像 → L2 事实 → L1 事件的证据链力导向图。
 *
 * - 颜色只表达「层」（四个主题各有一组经 dataviz 校验的分类色，见 globals.css
 *   的 --graph-l*）；断链节点用状态色 danger + 虚线描边 + 标签，不单靠颜色。
 * - 画布外给一份「节点清单」：同样的点击展开语义，兼作无障碍表视图与 E2E 锚点。
 * - 只有 L2/L3 条目（mem-…）可展开（后端 entry 参数只认这个形状）。
 * - 卡片外壳（标题/模式切换/返回按钮）在 GraphCard，本组件只画证据链本体。
 */

import { useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { layerLabelKey, previewText } from "@/lib/memory";
import { layoutGraph } from "@/lib/memory-graph";
import type { MemoryGraphNode, MemoryGraphPayload } from "@/types/api";

interface Props {
  payload: MemoryGraphPayload | undefined;
  isLoading: boolean;
  error: string | null;
  rootId: string | null;
  onExpand: (id: string) => void;
}

const LAYER_FILL: Record<string, string> = {
  l3: "var(--graph-l3)",
  l2: "var(--graph-l2)",
  l1: "var(--graph-l1)",
};

function isExpandable(node: MemoryGraphNode): boolean {
  return !node.broken && (node.layer === "l2" || node.layer === "l3") && node.id.startsWith("mem-");
}

export default function MemoryGraph({ payload, isLoading, error, rootId, onExpand }: Props) {
  const { t } = useI18n();
  const [hovered, setHovered] = useState<string | null>(null);

  const layout = useMemo(() => layoutGraph(payload?.nodes ?? [], payload?.edges ?? []), [payload]);
  const showLabels = layout.nodes.length <= 48;

  return (
    <div>
      {/* 图例：颜色 + 文字双通道（≥2 系列必须有图例） */}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        {(["l3", "l2", "l1"] as const).map((layer) => (
          <span key={layer} className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block size-2.5 rounded-full"
              style={{ background: LAYER_FILL[layer] }}
            />
            {t(layerLabelKey(layer))}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block size-2.5 rounded-full border border-dashed border-danger bg-surface"
          />
          {t("memory.graphBroken")}
        </span>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="py-8 text-center text-sm text-danger">{error}</p>
      ) : layout.nodes.length === 0 ? (
        <p className="py-12 text-center text-sm text-muted">{t("memory.graphEmpty")}</p>
      ) : (
        <svg
          data-testid="memory-graph-svg"
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          role="img"
          aria-label={t("memory.graphTitle")}
          className="mt-2 h-[440px] w-full select-none"
        >
          <g>
            {layout.edges.map((edge) => (
              <line
                key={`${edge.source}->${edge.target}`}
                x1={edge.x1}
                y1={edge.y1}
                x2={edge.x2}
                y2={edge.y2}
                style={{ stroke: "var(--border)" }}
                strokeWidth={2}
              />
            ))}
          </g>
          {layout.nodes.map((node) => {
            const expandable = isExpandable(node);
            const isRoot = node.id === rootId;
            return (
              <g
                key={node.id}
                data-testid="graph-node"
                data-id={node.id}
                data-layer={node.layer}
                data-broken={node.broken ? "true" : "false"}
                onClick={expandable ? () => onExpand(node.id) : undefined}
                onMouseEnter={() => setHovered(node.id)}
                onMouseLeave={() => setHovered(null)}
                className={expandable ? "cursor-pointer" : undefined}
              >
                <title>{node.text}</title>
                {isRoot && (
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={node.r + 5}
                    fill="none"
                    style={{ stroke: "var(--primary)" }}
                    strokeWidth={2}
                  />
                )}
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={node.r}
                  style={{
                    fill: node.broken ? "var(--surface)" : LAYER_FILL[node.layer],
                    stroke: node.broken
                      ? "var(--danger)"
                      : hovered === node.id
                        ? "var(--primary)"
                        : "var(--surface)",
                  }}
                  strokeWidth={2}
                  strokeDasharray={node.broken ? "3 3" : undefined}
                />
                {(showLabels || hovered === node.id || isRoot) && (
                  <text
                    x={node.x}
                    y={node.y + node.r + 13}
                    textAnchor="middle"
                    fontSize={11}
                    style={{ fill: "var(--foreground)" }}
                  >
                    {node.label}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      )}

      {/* 节点清单：与图同一份数据、同一套点击语义（表视图 / 无障碍 / E2E 锚点） */}
      {layout.nodes.length > 0 && (
        <ul data-testid="graph-node-list" className="mt-3 max-h-56 space-y-1 overflow-y-auto">
          {payload?.nodes.map((node) => {
            const expandable = isExpandable(node);
            return (
              <li key={node.id}>
                <button
                  type="button"
                  data-testid="graph-node-row"
                  data-id={node.id}
                  data-layer={node.layer}
                  disabled={!expandable}
                  onClick={() => onExpand(node.id)}
                  className="flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-accent disabled:cursor-default disabled:hover:bg-transparent"
                >
                  <span
                    aria-hidden
                    className="mt-0.5 inline-block size-2.5 shrink-0 rounded-full"
                    style={
                      node.broken
                        ? { border: "1px dashed var(--danger)" }
                        : { background: LAYER_FILL[node.layer] }
                    }
                  />
                  <span className="shrink-0 text-muted">{t(layerLabelKey(node.layer))}</span>
                  <span className="min-w-0 flex-1 break-words">
                    {previewText(node.text, 90)}
                    {node.broken && (
                      <span className="ml-1 text-danger">{t("memory.graphBroken")}</span>
                    )}
                  </span>
                  {node.date && <span className="shrink-0 text-muted">{node.date}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
