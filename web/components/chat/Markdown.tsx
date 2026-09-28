"use client";

/** Markdown 渲染（§4）：GFM + LaTeX（KaTeX）+ 代码块样式；mermaid/shiki 随 P7 补齐。 */

import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { createThrottle } from "@/lib/throttle";
import "katex/dist/katex.min.css";

/** 流式正文节流（§7.21 增量渲染）：增量再密也最多每 `ms` 重渲染一次。
 *
 * 这里必须是**节流**：防抖会在「增量间隔短于窗口」时把更新一路顺延，正文直到流
 * 结束才整段出现（真机踩过）。见 `lib/throttle.ts`。 */
export function useThrottledText(text: string, ms = 90): string {
  const [display, setDisplay] = useState(text);
  const throttle = useMemo(() => createThrottle<[string]>((value) => setDisplay(value), ms), [ms]);
  useEffect(() => {
    throttle.call(text);
    return () => throttle.cancel();
  }, [throttle, text]);
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
