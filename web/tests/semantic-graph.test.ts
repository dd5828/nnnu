import { describe, expect, it } from "vitest";
import {
  ENTITY_FILL,
  ENTITY_TYPES,
  entityFill,
  entityTypeLabelKey,
  layoutSemanticGraph,
} from "@/lib/semantic-graph";
import type { SemanticGraphEdge, SemanticGraphNode } from "@/types/api";

function node(
  name: string,
  type = "topic",
  extra: Partial<SemanticGraphNode> = {}
): SemanticGraphNode {
  return {
    id: name,
    name,
    type,
    refs: ["mem-1111aaaa"],
    count: 1,
    samples: ["证据正文"],
    ...extra,
  };
}

function edge(source: string, target: string, type = "正在学习"): SemanticGraphEdge {
  return { source, target, type, refs: ["mem-1111aaaa"], count: 1 };
}

describe("实体类型映射", () => {
  it("五种类型的颜色与文案键齐全", () => {
    for (const type of ENTITY_TYPES) {
      expect(ENTITY_FILL[type]).toBeTruthy();
      expect(entityTypeLabelKey(type)).toBe(
        `memory.entity${type[0].toUpperCase()}${type.slice(1)}`
      );
    }
  });

  it("未知类型回落到 other（不吞不炸）", () => {
    expect(entityFill("alien")).toBe(ENTITY_FILL.other);
    expect(entityTypeLabelKey("alien")).toBe("memory.entityOther");
  });
});

describe("语义图布局", () => {
  const nodes = [node("信号处理"), node("采样定理"), node("傅里叶")];
  const edges = [edge("信号处理", "采样定理"), edge("采样定理", "傅里叶")];

  it("同一输入同一输出（确定性）", () => {
    const a = layoutSemanticGraph(nodes, edges);
    const b = layoutSemanticGraph(nodes, edges);
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
  });

  it("节点 id 用实体名、半径沿用 L2 档（12px）、标签是名字", () => {
    const layout = layoutSemanticGraph(nodes, edges);
    const byId = new Map(layout.nodes.map((item) => [item.id, item]));
    for (const entity of nodes) {
      const placed = byId.get(entity.name)!;
      expect(placed.r).toBe(12);
      expect(placed.label).toBe(entity.name);
    }
  });

  it("悬空关系（端点没实体）被剔除，正常关系保留", () => {
    const layout = layoutSemanticGraph(nodes, [...edges, edge("信号处理", "不存在实体")]);
    expect(layout.edges.map((item) => `${item.source}->${item.target}`)).toEqual([
      "信号处理->采样定理",
      "采样定理->傅里叶",
    ]);
  });

  it("空输入不炸", () => {
    const layout = layoutSemanticGraph([], []);
    expect(layout.nodes).toEqual([]);
    expect(layout.edges).toEqual([]);
  });

  it("labelLimit 透传给标签截断", () => {
    const layout = layoutSemanticGraph([node("一个特别长的实体名字超过限制")], [], {
      labelLimit: 6,
    });
    expect(layout.nodes[0].label).toBe("一个特别长的…");
  });
});
