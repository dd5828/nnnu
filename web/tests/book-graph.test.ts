import { describe, expect, it } from "vitest";
import {
  GROUP_FILL_TOKENS,
  conceptGroups,
  edgeLabel,
  groupFill,
  layoutConceptGraph,
  parseConceptGraph,
} from "@/lib/book-graph";

describe("概念图解析", () => {
  it("parseConceptGraph 丢掉坏节点、悬空边与自环", () => {
    const graph = parseConceptGraph({
      nodes: [
        { id: "n1", label: "频率", group: "基础" },
        { id: "n2", label: "时间", group: "基础" },
        { id: "", label: "没 id" },
        { id: "n3", label: "" },
      ],
      edges: [
        { source: "n1", target: "n2", label: "对偶" },
        { source: "n1", target: "n9", label: "悬空" },
        { source: "n1", target: "n1", label: "自环" },
      ],
    });
    expect(graph.nodes.map((node) => node.id)).toEqual(["n1", "n2"]);
    expect(graph.edges).toEqual([{ source: "n1", target: "n2", label: "对偶" }]);
  });

  it("非对象/非数组一律当空", () => {
    expect(parseConceptGraph({}).nodes).toEqual([]);
    expect(parseConceptGraph({ nodes: "坏", edges: 5 }).edges).toEqual([]);
  });
});

describe("分组与配色", () => {
  const nodes = [
    { id: "n1", label: "A", group: "基础" },
    { id: "n2", label: "B", group: "进阶" },
    { id: "n3", label: "C", group: "基础" },
  ];

  it("conceptGroups 按首现序去重，空分组不算", () => {
    expect(conceptGroups(nodes)).toEqual(["基础", "进阶"]);
    expect(conceptGroups([{ id: "x", label: "x", group: "" }])).toEqual([]);
  });

  it("groupFill 按分组序取色轮，未知分组回第一色", () => {
    const groups = conceptGroups(nodes);
    expect(groupFill("基础", groups)).toBe(GROUP_FILL_TOKENS[0]);
    expect(groupFill("进阶", groups)).toBe(GROUP_FILL_TOKENS[1]);
    expect(groupFill("没这个组", groups)).toBe(GROUP_FILL_TOKENS[0]);
  });

  it("分组多于色数时轮转", () => {
    const many = GROUP_FILL_TOKENS.map((_, index) => `g${index}`).concat("g0x");
    expect(groupFill("g0x", many)).toBe(GROUP_FILL_TOKENS[0]);
  });
});

describe("概念图布局", () => {
  const graph = {
    nodes: [
      { id: "n1", label: "频率", group: "基础" },
      { id: "n2", label: "时间", group: "基础" },
      { id: "n3", label: "对偶", group: "进阶" },
    ],
    edges: [
      { source: "n1", target: "n2", label: "对偶" },
      { source: "n2", target: "n3", label: "延伸" },
    ],
  };

  it("确定性：同一输入同一输出", () => {
    const a = layoutConceptGraph(graph);
    const b = layoutConceptGraph(graph);
    expect(a).toEqual(b);
    expect(a.nodes.map((node) => node.id)).toEqual(["n1", "n2", "n3"]);
    expect(a.edges).toHaveLength(2);
  });

  it("布局丢的边与解析口径一致（下标对得上标签）", () => {
    const dangling = {
      nodes: [{ id: "n1", label: "孤立", group: "" }],
      edges: [{ source: "n1", target: "n9", label: "不该出现" }],
    };
    expect(layoutConceptGraph(dangling).edges).toEqual([]);
  });

  it("edgeLabel 折叠空白并截断", () => {
    expect(edgeLabel("对  偶\n关系")).toBe("对 偶 关系");
    expect(edgeLabel("一二三四五六七八九十十一")).toBe("一二三四五六七八九十…");
  });
});
