"use client";

/** Markdown 渲染（§4）：GFM + LaTeX（KaTeX）+ 代码块样式；mermaid/shiki 随 P7 补齐。 */

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

/** 流式渲染防抖：正文高频增量时不逐字重渲染（§7.21 增量渲染防抖）。 */
export function useDebouncedText(text: string, ms = 120): string {
  const [display, setDisplay] = useState(text);
  useEffect(() => {
    const timer = setTimeout(() => setDisplay(text), ms);
    return () => clearTimeout(timer);
  }, [text, ms]);
  return display;
}

export default function Markdown({ text }: { text: string }) {
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
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
