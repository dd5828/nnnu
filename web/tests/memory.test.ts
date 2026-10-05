import { describe, expect, it } from "vitest";
import {
  eventBadgeClass,
  eventLabelKey,
  formatBytes,
  formatTs,
  l1Summary,
  layerLabelKey,
  l3DocLabelKey,
  originLabelKey,
  previewText,
  refLayer,
  runStatusLabelKey,
  surfaceLabelKey,
} from "@/lib/memory";
import type { MemoryL1Row } from "@/types/api";

function row(event: string, data: Record<string, unknown> = {}): MemoryL1Row {
  return { ts: 1759000000, surface: "chat", session_id: "sess-1", event, data };
}

describe("i18n 键映射", () => {
  it("各层/面/文档/事件/来源/运行状态的键", () => {
    expect(surfaceLabelKey("deep_research")).toBe("memory.surface.deep_research");
    expect(l3DocLabelKey("profile")).toBe("memory.doc.profile");
    expect(layerLabelKey("l1")).toBe("memory.layer.l1");
    expect(eventLabelKey("tool_call")).toBe("memory.event.tool_call");
    expect(originLabelKey("human")).toBe("memory.origin.human");
    expect(runStatusLabelKey("ok")).toBe("memory.run.ok");
  });

  it("事件徽标按种类分色，未知种类回退中性样式", () => {
    expect(eventBadgeClass("user_message")).toContain("graph-l1");
    expect(eventBadgeClass("tool_call")).toContain("graph-l2");
    expect(eventBadgeClass("assistant_done")).toContain("graph-l3");
    expect(eventBadgeClass("cost")).toContain("text-muted");
    expect(eventBadgeClass("nope")).toContain("text-muted");
  });
});

describe("L1 行摘要", () => {
  it("用户消息与回复取正文", () => {
    expect(l1Summary(row("user_message", { text: "讲讲傅里叶" }))).toBe("讲讲傅里叶");
    expect(l1Summary(row("assistant_done", { text: "从直觉讲起" }))).toBe("从直觉讲起");
  });

  it("工具调用给「名: 摘要」，失败加叉", () => {
    expect(l1Summary(row("tool_call", { tool_name: "web_search", summary: "找到 3 条" }))).toBe(
      "web_search: 找到 3 条"
    );
    // 只有 args_preview 时用它兜底
    expect(l1Summary(row("tool_call", { tool_name: "exec", args_preview: "ls" }))).toBe("exec: ls");
    expect(l1Summary(row("tool_call", { tool_name: "exec", ok: false }))).toBe("exec · ✗");
  });

  it("追问合并问题与回答", () => {
    const data = { question: "走哪个方向？", answered: true, answer: "工程应用" };
    expect(l1Summary(row("ask_user", data))).toBe("走哪个方向？ → 工程应用");
    expect(l1Summary(row("ask_user", { question: "走哪个方向？" }))).toBe("走哪个方向？");
  });

  it("成本给 tokens 与花费", () => {
    expect(l1Summary(row("cost", { tokens: 1200, cost: 0.0123 }))).toBe("1200 tokens · $0.0123");
  });

  it("字段缺失不抛，未知事件回退原始名", () => {
    expect(l1Summary(row("tool_call", {}))).toBe("");
    expect(l1Summary(row("brand_new", { text: "x" }))).toBe("x");
    expect(l1Summary(row("brand_new", {}))).toBe("brand_new");
  });
});

describe("文本与格式化", () => {
  it("previewText 折叠空白并截断", () => {
    expect(previewText("a\n\n  b\tc")).toBe("a b c");
    expect(previewText("长".repeat(20), 10)).toBe(`${"长".repeat(10)}…`);
  });

  it("formatBytes 三档", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(-5)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
  });

  it("formatTs 无效值给空串", () => {
    expect(formatTs(0, "zh")).toBe("");
    expect(formatTs(Number.NaN, "zh")).toBe("");
    expect(formatTs(1759000000, "zh")).not.toBe("");
  });

  it("refLayer 识别引用前缀", () => {
    expect(refLayer("L1:chat/2026-09.jsonl#3")).toBe("l1");
    expect(refLayer("L2:chat#mem-1a2b3c4d")).toBe("l2");
    expect(refLayer("nope")).toBe("");
  });
});
