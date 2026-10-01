"use client";

/**
 * 语义知识图谱（§7.10 补做）：实体（人物/主题/项目/偏好/其他）+ 有向关系。
 *
 * - 布局复用证据链的 `layoutGraph`（见 lib/semantic-graph.ts），本组件只管渲染；
 * - 颜色只表达实体类型，且类型必须有文字二级编码（图例 + 实体行标签 + 连线
 *   tooltip 三级常驻）——dataviz 校验：粉↔青绿 CVD ΔE 落在地板带 6–8，文字不许删；
 * - 画布外同样给实体/关系清单：表视图、无障碍与 E2E 锚点。
 */

import { useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { previewText } from "@/lib/memory";
import {
  ENTITY_TYPES,
  entityFill,
  entityTypeLabelKey,
  layoutSemanticGraph,
} from "@/lib/semantic-graph";
import type { SemanticGraphPayload } from "@/types/api";

interface Props {
  payload: SemanticGraphPayload | undefined;
  isLoading: boolean;
  error: string | null;
}

export default function SemanticGraph({ payload, isLoading, error }: Props) {
  const { t } = useI18n();
  const [hovered, setHovered] = useState<string | null>(null);

  const nodes = payload?.nodes ?? [];
  const edges = payload?.edges ?? [];
  // 依赖整个 payload：`payload?.nodes ?? []` 每次渲染都是新数组，不能进 deps
  const layout = useMemo(
    () => layoutSemanticGraph(payload?.nodes ?? [], payload?.edges ?? []),
    [payload]
  );
  const showLabels = layout.nodes.length <= 48;

  const typeOf = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of payload?.nodes ?? []) {
      map.set(node.name, node.type);
    }
    return map;
  }, [payload]);

  // 同一对实体可能有多条不同类型的关系：连线 tooltip 合并成一个字段展示
  const edgeTypes = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const edge of payload?.edges ?? []) {
      const key = `${edge.source}\u0000${edge.target}`;
      map.set(key, [...(map.get(key) ?? []), edge.type]);
    }
    return map;
  }, [payload]);

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Loader2 className="h-4 w-4 animate-spin text-muted" />
      </div>
    );
  }
  if (error) {
    return <p className="py-8 text-center text-sm text-danger">{error}</p>;
  }
  if (nodes.length === 0) {
    return (
      <p data-testid="semantic-graph-empty" className="py-12 text-center text-sm text-muted">
        {t("memory.semanticEmpty")}
      </p>
    );
  }

  return (
    <>
      {payload?.stale && (
        <p
          data-testid="semantic-graph-stale"
          className="mt-2 rounded-md bg-accent px-2.5 py-1.5 text-xs text-muted"
        >
          {t("memory.semanticStale")}
        </p>
      )}

      {/* 图例与计数：类型色 + 文字（分类色不可单独承载身份） */}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        {ENTITY_TYPES.map((type) => (
          <span key={type} className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block size-2.5 rounded-full"
              style={{ background: entityFill(type) }}
            />
            {t(entityTypeLabelKey(type))}
          </span>
        ))}
        <span className="flex-1" />
        <span>{t("memory.semanticEntities", { n: String(nodes.length) })}</span>
        <span>{t("memory.semanticRelations", { n: String(edges.length) })}</span>
      </div>

      <svg
        data-testid="semantic-graph-svg"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="img"
        aria-label={t("memory.graphModeSemantic")}
        className="mt-2 h-[440px] w-full select-none"
      >
        <defs>
          <marker
            id="semantic-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 1 L 9 5 L 0 9 z" style={{ fill: "var(--muted)" }} />
          </marker>
        </defs>
        <g>
          {layout.edges.map((edge) => {
            const types = edgeTypes.get(`${edge.source}\u0000${edge.target}`) ?? [];
            return (
              <line
                key={`${edge.source}->${edge.target}`}
                x1={edge.x1}
                y1={edge.y1}
                x2={edge.x2}
                y2={edge.y2}
                style={{ stroke: "var(--border)" }}
                strokeWidth={2}
                markerEnd="url(#semantic-arrow)"
              >
                <title>{`${edge.source} —${types.join(" / ")}→ ${edge.target}`}</title>
              </line>
            );
          })}
        </g>
        {layout.nodes.map((node) => (
          <g
            key={node.id}
            data-testid="semantic-graph-node"
            data-id={node.id}
            data-type={typeOf.get(node.id) ?? "other"}
            onMouseEnter={() => setHovered(node.id)}
            onMouseLeave={() => setHovered(null)}
          >
            <title>{node.id}</title>
            <circle
              cx={node.x}
              cy={node.y}
              r={node.r}
              style={{
                fill: entityFill(typeOf.get(node.id) ?? "other"),
                stroke: hovered === node.id ? "var(--primary)" : "var(--surface)",
              }}
              strokeWidth={2}
            />
            {(showLabels || hovered === node.id) && (
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
        ))}
      </svg>

      {/* 实体清单：类型文字标签是颜色之外的第二通道 */}
      <ul data-testid="semantic-entity-list" className="mt-3 max-h-56 space-y-1 overflow-y-auto">
        {nodes.map((node) => (
          <li
            key={node.name}
            data-testid="semantic-entity-row"
            data-type={node.type}
            className="flex items-start gap-2 rounded-lg px-2 py-1.5 text-xs"
          >
            <span
              aria-hidden
              className="mt-0.5 inline-block size-2.5 shrink-0 rounded-full"
              style={{ background: entityFill(node.type) }}
            />
            <span className="shrink-0 font-medium">{node.name}</span>
            <span className="shrink-0 text-muted">{t(entityTypeLabelKey(node.type))}</span>
            <span className="min-w-0 flex-1 break-words text-muted">
              {node.samples[0] ? previewText(node.samples[0], 60) : ""}
            </span>
            <span className="shrink-0 text-muted">
              {t("memory.semanticRefs", { n: String(node.count) })}
            </span>
          </li>
        ))}
      </ul>

      {/* 关系清单：有向（—类型→），关系短语不靠图形单独表达 */}
      <ul data-testid="semantic-relation-list" className="mt-2 max-h-40 space-y-1 overflow-y-auto">
        {edges.map((edge) => (
          <li
            key={`${edge.source}->${edge.target}:${edge.type}`}
            data-testid="semantic-relation-row"
            className="flex items-start gap-2 rounded-lg px-2 py-1 text-xs text-muted"
          >
            <span className="shrink-0 text-foreground">{edge.source}</span>
            <span className="shrink-0">—{edge.type}→</span>
            <span className="shrink-0 text-foreground">{edge.target}</span>
            <span className="flex-1" />
            <span className="shrink-0">{t("memory.semanticRefs", { n: String(edge.count) })}</span>
          </li>
        ))}
      </ul>
    </>
  );
}
