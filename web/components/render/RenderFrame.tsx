"use client";

/** 渲染块外壳（§7.7）：标题条 + 全屏 + 下载 + 存笔记本，四种 viewer 共用一套。
 *
 * 全屏是把 viewer 再挂一次（portal 里重建实例）——ECharts/Mermaid 都有各自的容器，
 * 重建比搬 DOM 干净：搬过去容易踩 canvas / svg 尺寸与 ResizeObserver 的坑。
 */

import { useState } from "react";
import { Download, Maximize2 } from "lucide-react";
import SaveToNotebook from "@/components/chat/SaveToNotebook";
import { useI18n } from "@/hooks/useI18n";
import { downloadText } from "@/lib/download";
import { RENDER_FILE_EXT, toFenced, type RenderKind } from "@/lib/render";
import RenderLightbox from "./RenderLightbox";

export const RENDER_LABEL_KEYS: Record<RenderKind, string> = {
  svg: "render.kindSvg",
  echarts: "render.kindEcharts",
  mermaid: "render.kindMermaid",
  html: "render.kindHtml",
};

export default function RenderFrame({
  kind,
  code,
  children,
}: {
  kind: RenderKind;
  code: string;
  children: React.ReactNode;
}) {
  const { t } = useI18n();
  const [fullscreen, setFullscreen] = useState(false);
  const title = t(RENDER_LABEL_KEYS[kind]);

  const download = () => {
    const stamp = new Date().toISOString().slice(0, 10);
    downloadText(code, `${t("render.fileStem")}-${kind}-${stamp}.${RENDER_FILE_EXT[kind]}`);
  };

  return (
    <div
      data-testid="render-frame"
      data-kind={kind}
      className="my-2 flex w-full flex-col overflow-hidden rounded-xl border border-border bg-surface"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-[11px] text-muted">
        <span className="min-w-0 flex-1 truncate">{title}</span>
        <button
          type="button"
          data-testid="render-download"
          onClick={download}
          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 transition-colors hover:bg-accent hover:text-foreground"
        >
          <Download className="h-3 w-3" />
          {t("render.download")}
        </button>
        <button
          type="button"
          data-testid="render-fullscreen"
          onClick={() => setFullscreen(true)}
          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 transition-colors hover:bg-accent hover:text-foreground"
        >
          <Maximize2 className="h-3 w-3" />
          {t("render.fullscreen")}
        </button>
        <SaveToNotebook content={toFenced(kind, code)} />
      </div>
      <div className="w-full p-3">{children}</div>
      {fullscreen && (
        <RenderLightbox title={title} onClose={() => setFullscreen(false)}>
          <div className="w-full max-w-[1100px]">{children}</div>
        </RenderLightbox>
      )}
    </div>
  );
}
