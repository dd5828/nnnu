"use client";

/** Mermaid 渲染（§7.7）：`securityLevel: "strict"`——图内 HTML 标签与链接一律不生效，
 *  DSL 里写 `<script>` 也只是普通文本。动态 import，别让聊天首屏背上 mermaid 的体积。
 *
 * mermaid.render 失败时默认会往 body 里塞一张错误图（suppressErrorRendering 关掉），
 * 这里统一换成我们自己的内联错误卡。
 */

import { useEffect, useId, useState } from "react";
import RenderError from "./RenderError";

function isDarkTheme(): boolean {
  if (typeof document === "undefined") {
    return false;
  }
  const theme = document.documentElement.dataset.theme ?? "";
  return theme === "dark" || theme === "glass";
}

export default function MermaidViewer({ code, className }: { code: string; className?: string }) {
  const reactId = useId();
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const [themeTick, setThemeTick] = useState(0);

  useEffect(() => {
    const observer = new MutationObserver(() => setThemeTick((tick) => tick + 1));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          suppressErrorRendering: true,
          theme: isDarkTheme() ? "dark" : "default",
          fontFamily: "inherit",
        });
        const renderId = `mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
        const result = await mermaid.render(renderId, code);
        if (!disposed) {
          setSvg(result.svg);
          setError("");
        }
      } catch (err) {
        if (!disposed) {
          setSvg("");
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      disposed = true;
    };
  }, [code, reactId, themeTick]);

  if (error) {
    return <RenderError kind="Mermaid" detail={error} />;
  }
  if (!svg) {
    return <div className="h-16 w-full animate-pulse rounded-lg bg-accent/50" />;
  }
  return (
    <div
      data-testid="mermaid-viewer"
      className={`flex w-full justify-center overflow-x-auto ${className ?? ""}`}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
