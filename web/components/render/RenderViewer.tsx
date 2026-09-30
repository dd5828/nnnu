"use client";

/** 渲染块入口（§7.7）：Markdown 的 ```svg / ```echarts / ```mermaid / ```html 围栏换成它。
 *  四个 viewer 的分发点——新的渲染类型只在这里加一行。
 */

import type { RenderKind } from "@/lib/render";
import EchartsViewer from "./EchartsViewer";
import HtmlViewer from "./HtmlViewer";
import MermaidViewer from "./MermaidViewer";
import RenderFrame from "./RenderFrame";
import SvgViewer from "./SvgViewer";

export default function RenderViewer({ kind, code }: { kind: RenderKind; code: string }) {
  return (
    <RenderFrame kind={kind} code={code}>
      {kind === "svg" && <SvgViewer code={code} />}
      {kind === "echarts" && <EchartsViewer code={code} />}
      {kind === "mermaid" && <MermaidViewer code={code} />}
      {kind === "html" && <HtmlViewer code={code} />}
    </RenderFrame>
  );
}
