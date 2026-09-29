/** 引用的落点计算（§6.1 citation 事件）：一条来源该渲染成什么。
 *
 * 从 `CitationsPanel` 里抽出来的纯函数——vitest 在本仓是 node 环境，只收
 * `tests/*.test.ts`，组件测跑不起来（P5 批三已有此结论），所以能测的部分必须
 * 先离开组件。
 *
 * 三支分流：
 * - **外链**：`doc_id` 是 http(s) 的（web_search / paper_search / web_fetch 都这么给）
 *   → 新窗口打开。旧逻辑把这类引用一律拼成 `/knowledge/web?doc=https%3A…`，指着一个
 *   不存在的知识库，用户点了就 404——P6 报告要求来源「可点击验证」，这条得先修好。
 * - **内链**：知识库引用（kb 是某个 kb_id）→ 知识中心详情页深链接 `?doc=&page=`。
 * - **纯文本**：附件引用（kb === "attachment"）与缺 kb 的引用，没有可跳的页面。
 *
 * `label` 只给「这是哪一份」的名字，页码后缀由调用方拼（要走 i18n 的 `chat.page`）。
 */

import type { CitationSource } from "@/types/stream";

export type CitationTarget =
  | { kind: "external"; href: string; label: string }
  | { kind: "internal"; href: string; label: string }
  | { kind: "text"; label: string };

/** `doc_id` 是不是一个可以直接打开的网址。 */
export function isExternalDoc(docId: string): boolean {
  return /^https?:\/\//i.test(docId.trim());
}

/** 外链的名字：标题优先，没有就退到域名（整条 URL 当标签会把面板撑爆）。 */
export function externalLabel(source: CitationSource): string {
  const title = source.title?.trim();
  if (title) {
    return title;
  }
  try {
    return new URL(source.doc_id).hostname || source.doc_id;
  } catch {
    return source.doc_id;
  }
}

export function citationTarget(source: CitationSource): CitationTarget {
  if (isExternalDoc(source.doc_id)) {
    return { kind: "external", href: source.doc_id.trim(), label: externalLabel(source) };
  }
  const hasPage = source.page !== null && source.page !== undefined;
  const title = source.title?.trim();
  if (!source.kb || source.kb === "attachment") {
    return { kind: "text", label: title || source.doc_id };
  }
  const query = new URLSearchParams({ doc: source.doc_id });
  if (hasPage) {
    query.set("page", String(source.page));
  }
  return {
    kind: "internal",
    href: `/knowledge/${source.kb}?${query.toString()}`,
    label: title || source.kb,
  };
}
