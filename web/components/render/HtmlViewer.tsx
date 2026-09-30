"use client";

/** 单文件 HTML 渲染（§7.7）：srcdoc + `sandbox="allow-scripts"`。
 *
 * **不给 allow-same-origin**：脚本因此跑在 null origin 里，碰不到父页面的 localStorage /
 * cookie / DOM（提示词里也是这么向模型承诺的）。KaTeX 由宿主注入（页面只写 $...$ / $$...$$）：
 * 模型产出的 HTML 里不许自带 `</head>` 之外的东西，注入位置挑 head 末尾。
 */

import { useMemo } from "react";

const KATEX_SNIPPET = [
  '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">',
  '<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>',
  '<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>',
  "<script>document.addEventListener('DOMContentLoaded',function(){",
  "if(window.renderMathInElement){renderMathInElement(document.body,{delimiters:[",
  '{left:"$$",right:"$$",display:true},{left:"$",right:"$",display:false}]});}});</script>',
].join("");

/** 把 KaTeX 注入页面（没有 head 的页面退而求其次插到 body 前 / 最前面）。 */
export function withKatex(html: string): string {
  if (/<\/head>/i.test(html)) {
    return html.replace(/<\/head>/i, `${KATEX_SNIPPET}</head>`);
  }
  if (/<body[^>]*>/i.test(html)) {
    return html.replace(/<body[^>]*>/i, (tag) => `${KATEX_SNIPPET}${tag}`);
  }
  return KATEX_SNIPPET + html;
}

export default function HtmlViewer({ code, className }: { code: string; className?: string }) {
  const srcdoc = useMemo(() => withKatex(code), [code]);
  return (
    <iframe
      data-testid="html-viewer"
      title="html-preview"
      sandbox="allow-scripts"
      srcDoc={srcdoc}
      className={`w-full rounded-lg border border-border bg-surface ${className ?? "h-[420px]"}`}
    />
  );
}
