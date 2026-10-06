/** 引用落点（§6.1 / §7.6 验收「来源可点击验证」）：外链 / 知识库深链 / 纯文本三分支。 */

import { describe, expect, it } from "vitest";

import {
  citationTarget,
  externalLabel,
  isExternalDoc,
  kbDeepLink,
  refTarget,
} from "@/lib/citations";
import type { RefEntry } from "@/lib/refs";
import type { CitationSource } from "@/types/stream";

function source(overrides: Partial<CitationSource> = {}): CitationSource {
  return { doc_id: "doc-1", kb: "kb-abc", page: null, snippet: "", ...overrides };
}

describe("isExternalDoc", () => {
  it("只认 http(s) 开头的 doc_id", () => {
    expect(isExternalDoc("https://example.com/a")).toBe(true);
    expect(isExternalDoc("HTTP://example.com")).toBe(true);
    expect(isExternalDoc("kbdoc-1234")).toBe(false);
    expect(isExternalDoc("att-abcd1234")).toBe(false);
    expect(isExternalDoc("https:/example.com")).toBe(false); // 少一个斜杠不算
  });
});

describe("citationTarget 外链分支", () => {
  it("web 引用渲染成新窗口能开的链接，标签用标题", () => {
    const target = citationTarget(
      source({ doc_id: "https://arxiv.org/abs/2401.1", kb: "arxiv", title: "RAG 综述" })
    );
    expect(target).toEqual({
      kind: "external",
      href: "https://arxiv.org/abs/2401.1",
      label: "RAG 综述",
    });
  });

  it("没有标题时退到域名，不是整条 URL", () => {
    const target = citationTarget(
      source({ doc_id: "https://www.example.com/a/very/long/path?q=1", kb: "web" })
    );
    expect(target.kind).toBe("external");
    expect(target.label).toBe("www.example.com");
  });

  it("doc_id 是外链时不再拼知识库路径（旧逻辑的 404 就出在这儿）", () => {
    const target = citationTarget(source({ doc_id: "https://example.com", kb: "web" }));
    expect(target.kind === "external" && target.href).not.toContain("/knowledge/");
  });

  it("标题只有空白等同于没有标题", () => {
    expect(externalLabel(source({ doc_id: "https://a.io/x", title: "   " }))).toBe("a.io");
  });
});

describe("citationTarget 知识库分支", () => {
  it("深链接带上 doc 与 page", () => {
    const target = citationTarget(source({ kb: "kb-abc", doc_id: "kbdoc-1", page: 7 }));
    expect(target.kind).toBe("internal");
    expect(target.kind === "internal" && target.href).toBe("/knowledge/kb-abc?doc=kbdoc-1&page=7");
  });

  it("没有页码时不带 page 参数", () => {
    const target = citationTarget(source({ kb: "kb-abc", doc_id: "kbdoc-1", page: null }));
    expect(target.kind === "internal" && target.href).toBe("/knowledge/kb-abc?doc=kbdoc-1");
  });

  it("标签优先用标题（文件名），没有才退回库 id", () => {
    expect(citationTarget(source({ kb: "kb-abc", title: "教材.pdf" })).label).toBe("教材.pdf");
    expect(citationTarget(source({ kb: "kb-abc" })).label).toBe("kb-abc");
  });
});

describe("kbDeepLink（引用与全库检索共用的深链接）", () => {
  it("带页码与不带页码两种形状", () => {
    expect(kbDeepLink("kb-abc", "kbdoc-1", 7)).toBe("/knowledge/kb-abc?doc=kbdoc-1&page=7");
    expect(kbDeepLink("kb-abc", "kbdoc-1", null)).toBe("/knowledge/kb-abc?doc=kbdoc-1");
    expect(kbDeepLink("kb-abc", "kbdoc-1", undefined)).toBe("/knowledge/kb-abc?doc=kbdoc-1");
  });

  it("与 citationTarget 走同一条路径（两处口径不许分叉）", () => {
    const target = citationTarget(source({ kb: "kb-abc", doc_id: "kbdoc-1", page: 7 }));
    expect(target.kind === "internal" && target.href).toBe(kbDeepLink("kb-abc", "kbdoc-1", 7));
  });
});

describe("citationTarget 纯文本分支", () => {
  it("附件引用不跳转，标签用附件名", () => {
    const target = citationTarget(
      source({ kb: "attachment", doc_id: "att-1", page: 2, title: "讲义.pdf" })
    );
    expect(target).toEqual({ kind: "text", label: "讲义.pdf" });
  });

  it("缺 kb 的引用也当纯文本，不拼出不存在的知识库路径", () => {
    const target = citationTarget(source({ kb: "", doc_id: "kbdoc-9" }));
    expect(target).toEqual({ kind: "text", label: "kbdoc-9" });
  });
});

describe("refTarget（用户消息的引用 chip 落点）", () => {
  it("笔记本记录 → 笔记本页 ?record= 深链", () => {
    const entry: RefEntry = {
      kind: "notebook_record",
      notebook_id: "nb-1",
      record_id: "nbr-2",
      label: "极限笔记",
      resolved: true,
    };
    expect(refTarget(entry)).toEqual({
      kind: "internal",
      href: "/notebooks/nb-1?record=nbr-2",
      label: "极限笔记",
    });
  });

  it("题目 → 题库页 ?question= 置顶", () => {
    const entry: RefEntry = {
      kind: "question",
      question_id: "q-1",
      label: "下列极限",
      resolved: true,
    };
    expect(refTarget(entry)).toEqual({
      kind: "internal",
      href: "/questions?question=q-1",
      label: "下列极限",
    });
  });

  it("已解析但 label 为空：退到 id 当标签", () => {
    const entry: RefEntry = {
      kind: "question",
      question_id: "q-9",
      label: "",
      resolved: true,
    };
    expect(refTarget(entry).label).toBe("q-9");
  });

  it("已删（resolved=false）→ 纯文本，不造死链", () => {
    const entry: RefEntry = {
      kind: "question",
      question_id: "q-9",
      label: "旧题",
      resolved: false,
    };
    expect(refTarget(entry)).toEqual({ kind: "text", label: "旧题" });
  });

  it("已删的笔记本记录（快照没带归属）→ 纯文本", () => {
    const entry: RefEntry = {
      kind: "notebook_record",
      notebook_id: "",
      record_id: "nbr-9",
      label: "",
      resolved: false,
    };
    expect(refTarget(entry)).toEqual({ kind: "text", label: "nbr-9" });
  });
});
