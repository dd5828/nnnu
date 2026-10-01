import { describe, expect, it } from "vitest";
import { layoutGraph, nodeRadius, truncateLabel } from "@/lib/memory-graph";
import type { MemoryGraphEdge, MemoryGraphNode } from "@/types/api";

function node(
  id: string,
  layer: "l1" | "l2" | "l3",
  extra: Partial<MemoryGraphNode> = {}
): MemoryGraphNode {
  return {
    id,
    layer,
    key: "chat",
    kind: "entry",
    text: `条目 ${id}`,
    date: "2026-09-18",
    ref: "",
    stale: false,
    edited: false,
    origin: "consolidator",
    broken: false,
    ...extra,
  };
}

function edge(source: string, target: string): MemoryGraphEdge {
  return { source, target };
}

describe("节点规格", () => {
  it("半径按层递减，断链同 L2 档", () => {
    expect(nodeRadius(node("a", "l3"))).toBe(16);
    expect(nodeRadius(node("b", "l2"))).toBe(12);
    expect(nodeRadius(node("c", "l1"))).toBe(9);
    expect(nodeRadius(node("d", "l1", { broken: true }))).toBe(11);
  });

  it("标签折叠空白并截断", () => {
    expect(truncateLabel("a\n b")).toBe("a b");
    expect(truncateLabel("长".repeat(30), 10)).toBe(`${"长".repeat(10)}…`);
  });
});

describe("力导向布局", () => {
  const nodes = [node("l3-1", "l3"), node("l2-1", "l2"), node("l1-1", "l1")];
  const edges = [edge("l3-1", "l2-1"), edge("l2-1", "l1-1")];

  it("同一输入同一输出（确定性，不靠随机）", () => {
    const a = layoutGraph(nodes, edges);
    const b = layoutGraph(nodes, edges);
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
  });

  it("坐标都在画布内且不是 NaN", () => {
    const layout = layoutGraph(nodes, edges);
    for (const item of layout.nodes) {
      expect(Number.isFinite(item.x)).toBe(true);
      expect(Number.isFinite(item.y)).toBe(true);
      expect(item.x).toBeGreaterThan(0);
      expect(item.x).toBeLessThan(layout.width);
      expect(item.y).toBeGreaterThan(0);
      expect(item.y).toBeLessThan(layout.height);
    }
  });

  it("单个节点居中", () => {
    const layout = layoutGraph([node("only", "l2")], []);
    expect(layout.nodes[0].x).toBe(380);
    expect(layout.nodes[0].y).toBe(220);
  });

  it("空图不炸", () => {
    const layout = layoutGraph([], []);
    expect(layout.nodes).toEqual([]);
    expect(layout.edges).toEqual([]);
  });

  it("边端点停在节点圆周上（不相交节点内部）", () => {
    const layout = layoutGraph(nodes, edges);
    const byId = new Map(layout.nodes.map((item) => [item.id, item]));
    for (const link of layout.edges) {
      const from = byId.get(link.source)!;
      const to = byId.get(link.target)!;
      expect(Math.hypot(link.x1 - from.x, link.y1 - from.y)).toBeCloseTo(from.r, 0);
      expect(Math.hypot(link.x2 - to.x, link.y2 - to.y)).toBeCloseTo(to.r, 0);
    }
  });

  it("未知端点/自环的边直接丢掉", () => {
    const layout = layoutGraph(nodes, [edge("l3-1", "nope"), edge("l2-1", "l2-1")]);
    expect(layout.edges).toEqual([]);
  });

  it("孤立节点被向心力留在画布内", () => {
    const many = Array.from({ length: 12 }, (_, index) => node(`iso-${index}`, "l2"));
    const layout = layoutGraph(many, []);
    for (const item of layout.nodes) {
      expect(item.x).toBeGreaterThan(0);
      expect(item.x).toBeLessThan(layout.width);
      expect(item.y).toBeGreaterThan(0);
      expect(item.y).toBeLessThan(layout.height);
    }
  });
});
