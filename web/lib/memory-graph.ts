/** 记忆图谱布局（§7.10 验收②）：纯函数力导向（Fruchterman-Reingold 简化版）。
 *
 * 为什么自研不拉库：图画布小（几十个节点）、要确定性（单测钉死同一输入同一
 * 输出，不靠随机种子）、且仓库既有的可视化约定是「纯函数 + 单测」（见 chat-rail）。
 * 力导向迭代在数据变化时一次性算完，不做逐帧物理循环。
 *
 * 渲染约定（dataviz 规则）：边 2px、节点直径 ≥16px、节点带 2px surface 描边；
 * 颜色只表达「层」，文字一律墨色 token，色觉障碍读者靠标签与图例辨认。
 */

import type { MemoryGraphEdge, MemoryGraphNode } from "@/types/api";

export interface GraphLayoutNode extends MemoryGraphNode {
  x: number;
  y: number;
  r: number;
  label: string;
}

export interface GraphLayoutEdge {
  source: string;
  target: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface GraphLayout {
  width: number;
  height: number;
  nodes: GraphLayoutNode[];
  edges: GraphLayoutEdge[];
}

/** 节点半径按层递减：L3 画像最大 → L2 事实 → L1 原始行；断链结点同 L2。 */
export function nodeRadius(node: Pick<MemoryGraphNode, "layer" | "broken">): number {
  if (node.broken) {
    return 11;
  }
  if (node.layer === "l3") {
    return 16;
  }
  if (node.layer === "l2") {
    return 12;
  }
  return 9;
}

/** 标签截断（图谱节点标题；悬停 tooltip 给全文）。 */
export function truncateLabel(text: string, limit = 18): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

interface Options {
  width?: number;
  height?: number;
  iterations?: number;
  labelLimit?: number;
}

/**
 * 计算布局：同一输入 + 同一参数 → 同一输出（初值按输入序摆圆，无随机）。
 * 边只连两端都存在的节点；越界坐标夹回画布。
 */
export function layoutGraph(
  nodes: MemoryGraphNode[],
  edges: MemoryGraphEdge[],
  options: Options = {}
): GraphLayout {
  const width = options.width ?? 760;
  const height = options.height ?? 440;
  const iterations = options.iterations ?? 240;
  const labelLimit = options.labelLimit ?? 18;

  const count = nodes.length;
  const points = nodes.map((_, index) => {
    if (count === 1) {
      return { x: width / 2, y: height / 2 };
    }
    const angle = (2 * Math.PI * index) / count - Math.PI / 2;
    const ring = Math.min(width, height) * 0.34;
    return {
      x: width / 2 + ring * Math.cos(angle),
      y: height / 2 + ring * Math.sin(angle),
    };
  });

  const byId = new Map<string, number>();
  nodes.forEach((node, index) => byId.set(node.id, index));
  const links: Array<[number, number]> = [];
  for (const edge of edges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (a !== undefined && b !== undefined && a !== b) {
      links.push([a, b]);
    }
  }

  if (count > 1) {
    const area = width * height;
    // 理想边长：标准 FR 取 sqrt(面积/节点数)，但节点少时会大过半个画布——封顶到
    // 0.35×短边，稀疏图才会聚在中间而不是被推到边界（大图不受影响）。
    const k = Math.min(Math.sqrt(area / count), Math.min(width, height) * 0.35);
    let temperature = Math.min(width, height) * 0.12;
    for (let step = 0; step < iterations; step += 1) {
      const disp = points.map(() => ({ x: 0, y: 0 }));
      for (let i = 0; i < count; i += 1) {
        for (let j = i + 1; j < count; j += 1) {
          const dx = points[i].x - points[j].x;
          const dy = points[i].y - points[j].y;
          const dist = Math.max(0.5, Math.hypot(dx, dy));
          const force = (k * k) / dist;
          disp[i].x += (dx / dist) * force;
          disp[i].y += (dy / dist) * force;
          disp[j].x -= (dx / dist) * force;
          disp[j].y -= (dy / dist) * force;
        }
      }
      for (const [a, b] of links) {
        const dx = points[a].x - points[b].x;
        const dy = points[a].y - points[b].y;
        const dist = Math.max(0.5, Math.hypot(dx, dy));
        const force = (dist * dist) / k;
        disp[a].x -= (dx / dist) * force;
        disp[a].y -= (dy / dist) * force;
        disp[b].x += (dx / dist) * force;
        disp[b].y += (dy / dist) * force;
      }
      for (let i = 0; i < count; i += 1) {
        // 弱向心：防止孤立分量漂到角落
        disp[i].x += (width / 2 - points[i].x) * 0.02;
        disp[i].y += (height / 2 - points[i].y) * 0.02;
        const length = Math.max(0.5, Math.hypot(disp[i].x, disp[i].y));
        const step = Math.min(length, temperature);
        const pad = nodeRadius(nodes[i]) + 10;
        points[i].x = clamp(points[i].x + (disp[i].x / length) * step, pad, width - pad);
        points[i].y = clamp(points[i].y + (disp[i].y / length) * step, pad, height - pad);
      }
      temperature = Math.max(0.5, temperature * (1 - (step + 1) / iterations));
    }

    // 收尾取景：力导向会把节点推到夹取边界（节点少的图尤其明显），直接渲染会贴边、
    // 居中标签还会出框——按「带标签余量的可用框」整体平移缩放（只缩不放），
    // 相对结构不变，仍是确定性结果。
    const padX = 100; // 标签最长 18 字、11px 居中挂节点下方，两侧各留半宽
    const padY = 32; // 底部标签基线（y + r + 13）要留在画布内
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const point of points) {
      minX = Math.min(minX, point.x);
      maxX = Math.max(maxX, point.x);
      minY = Math.min(minY, point.y);
      maxY = Math.max(maxY, point.y);
    }
    const scale = Math.min(
      1,
      (width - 2 * padX) / Math.max(1, maxX - minX),
      (height - 2 * padY) / Math.max(1, maxY - minY)
    );
    const centerX = (minX + maxX) / 2;
    const centerY = (minY + maxY) / 2;
    for (const point of points) {
      point.x = width / 2 + (point.x - centerX) * scale;
      point.y = height / 2 + (point.y - centerY) * scale;
    }
  }

  const layoutNodes: GraphLayoutNode[] = nodes.map((node, index) => ({
    ...node,
    x: round(points[index].x),
    y: round(points[index].y),
    r: nodeRadius(node),
    label: truncateLabel(node.text || node.id, labelLimit),
  }));

  const layoutEdges: GraphLayoutEdge[] = links.map(([a, b]) => {
    const from = points[a];
    const to = points[b];
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const dist = Math.max(0.5, Math.hypot(dx, dy));
    const ux = dx / dist;
    const uy = dy / dist;
    return {
      source: nodes[a].id,
      target: nodes[b].id,
      x1: round(from.x + ux * nodeRadius(nodes[a])),
      y1: round(from.y + uy * nodeRadius(nodes[a])),
      x2: round(to.x - ux * nodeRadius(nodes[b])),
      y2: round(to.y - uy * nodeRadius(nodes[b])),
    };
  });

  return { width, height, nodes: layoutNodes, edges: layoutEdges };
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}
