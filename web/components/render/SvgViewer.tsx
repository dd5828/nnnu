"use client";

/** SVG 内联渲染（§7.7）：消毒后 inject 进文档，跟着页面主题走（不画背景、颜色用预置 class）。
 *
 * 每张图一个 id 前缀：同一页出现两张图时，无前缀会让第二个 `<defs id="arrow">` 把第一个
 * 的箭头盖掉（引用 `url(#arrow)` 会解析到文档里第一个同名 id）。
 */

import { useId, useMemo } from "react";
import { sanitizeSvg } from "@/lib/svg-sanitize";
import RenderError from "./RenderError";

export default function SvgViewer({ code, className }: { code: string; className?: string }) {
  const reactId = useId();
  const result = useMemo(() => {
    try {
      return { html: sanitizeSvg(code, `svg${reactId}`), error: "" };
    } catch (err) {
      return { html: "", error: err instanceof Error ? err.message : String(err) };
    }
  }, [code, reactId]);

  if (result.error) {
    return <RenderError kind="SVG" detail={result.error} />;
  }
  return (
    <div
      data-testid="svg-viewer"
      className={`svg-host w-full ${className ?? ""}`}
      dangerouslySetInnerHTML={{ __html: result.html }}
    />
  );
}
