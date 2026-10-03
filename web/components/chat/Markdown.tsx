"use client";

/** Markdown 渲染（§4）：GFM + LaTeX（KaTeX）+ 代码块样式；
 *  ```svg / ```echarts / ```mermaid / ```html 围栏换成真渲染块（§7.7），```nnnu-artifact
 *  换成产物卡片（§7.8）——正文契约见 `lib/render.ts`。 */

import { isValidElement, memo, useEffect, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import ArtifactCard from "@/components/render/ArtifactCard";
import RenderViewer from "@/components/render/RenderViewer";
import { ARTIFACT_LANG, isRenderLang, kindFromLang, parseArtifactJson } from "@/lib/render";
import { createThrottle, STREAM_THROTTLE_MS } from "@/lib/throttle";
import "katex/dist/katex.min.css";

/** 流式正文节流（§7.21 增量渲染）：增量再密也最多每 `ms` 重渲染一次。
 *
 * 这里必须是**节流**：防抖会在「增量间隔短于窗口」时把更新一路顺延，正文直到流
 * 结束才整段出现（真机踩过）。见 `lib/throttle.ts`。 */
export function useThrottledText(text: string, ms = STREAM_THROTTLE_MS): string {
  const [display, setDisplay] = useState(text);
  const throttle = useMemo(() => createThrottle<[string]>((value) => setDisplay(value), ms), [ms]);
  useEffect(() => {
    throttle.call(text);
    return () => throttle.cancel();
  }, [throttle, text]);
  return display;
}

/** 把 React 节点摊成纯文本（找围栏语言用的那段代码就藏在 code 元素里）。 */
function plainText(node: ReactNode): string {
  if (typeof node === "string") {
    return node;
  }
  if (typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map((child) => plainText(child as ReactNode)).join("");
  }
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return plainText(node.props.children);
  }
  return "";
}

/** 块级代码：认得渲染围栏就换真渲染块，认得产物围栏就换卡片，其余照旧。 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const list = Array.isArray(children) ? children : [children];
  const codeElement = list.find((child) => isValidElement(child));
  const props = isValidElement<{ className?: string; children?: ReactNode }>(codeElement)
    ? codeElement.props
    : undefined;
  const lang = /language-([\w-]+)/.exec(props?.className ?? "")?.[1]?.toLowerCase() ?? "";
  if (isRenderLang(lang)) {
    const kind = kindFromLang(lang);
    if (kind !== null) {
      return <RenderViewer kind={kind} code={plainText(props?.children).trim()} />;
    }
  }
  if (lang === ARTIFACT_LANG) {
    const info = parseArtifactJson(plainText(props?.children));
    if (info !== null) {
      return <ArtifactCard info={info} />;
    }
  }
  return <pre>{children}</pre>;
}

// memo 是流式不抖的关键一击：react-markdown 内部零缓存，text 引用不变就直接跳过——
// 思考在流时正文的字符串引用根本没变，历史消息更是整条不动（见 tests 里的合并逻辑）
function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          a: ({ children, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export default memo(Markdown);
