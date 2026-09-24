"use client";

/**
 * DOCX 预览：docx-preview 把文档铺成带原样式的分页 HTML（比解析文本好读得多）。
 *
 * 库本身懒加载（await import），只在这类文档真被打开时才走网络。
 * 渲染后按视口宽度做缩放贴合：CSS zoom 是有布局感知的缩放，浏览器不认时
 * 还有外层 overflow-auto 兜底看全页。
 */

import { useEffect, useRef, useState } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useBinarySource } from "../useBinarySource";
import { PreviewFailure } from "./PreviewStatus";

const MIN_FIT_SCALE = 0.5;

function fitRenderedDocx(viewport: HTMLElement, host: HTMLElement) {
  const wrapper = host.querySelector<HTMLElement>(".docx-wrapper");
  const pages = Array.from(host.querySelectorAll<HTMLElement>("section.docx"));
  if (!wrapper || pages.length === 0) return;

  wrapper.style.setProperty("box-sizing", "border-box");
  wrapper.style.setProperty("width", "max-content");
  wrapper.style.setProperty("min-width", "100%");
  wrapper.style.setProperty("align-items", "center");
  wrapper.style.setProperty("padding", "16px 12px 0");
  wrapper.style.setProperty("transform-origin", "top center");

  // 先量原尺寸再缩放；浏览器忽略 zoom 时靠 overflow-auto 看全
  wrapper.style.removeProperty("zoom");
  const pageWidth = Math.max(...pages.map((page) => page.offsetWidth || 0));
  if (!pageWidth) return;

  const availableWidth = Math.max(viewport.clientWidth - 32, 240);
  const scale = Math.min(1, Math.max(MIN_FIT_SCALE, availableWidth / pageWidth));
  wrapper.style.setProperty("zoom", String(scale));
}

export default function DocxPreview({ url }: { url: string }) {
  const { t } = useI18n();
  const src = useBinarySource(url);
  const viewportRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [rendering, setRendering] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (src.kind !== "ready") return;
    const container = containerRef.current;
    if (!container) return;

    let cancelled = false;
    let resizeObserver: ResizeObserver | null = null;
    let frame = 0;
    const scheduleFit = () => {
      if (cancelled) return;
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const viewport = viewportRef.current;
        if (viewport && containerRef.current) {
          fitRenderedDocx(viewport, containerRef.current);
        }
      });
    };

    setRendering(true);
    setFailed(false);
    (async () => {
      try {
        const { renderAsync } = await import("docx-preview");
        if (cancelled) return;
        container.innerHTML = "";
        await renderAsync(src.buffer, container, undefined, {
          className: "docx",
          inWrapper: true,
          breakPages: true,
          ignoreLastRenderedPageBreak: true,
          useBase64URL: true,
        });
        if (cancelled) return;
        scheduleFit();
        const viewport = viewportRef.current;
        if (viewport) {
          resizeObserver = new ResizeObserver(scheduleFit);
          resizeObserver.observe(viewport);
        }
      } catch {
        if (!cancelled) setFailed(true);
      } finally {
        if (!cancelled) setRendering(false);
      }
    })();

    return () => {
      cancelled = true;
      resizeObserver?.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [src]);

  if (src.kind === "error") {
    return <PreviewFailure code={src.code} />;
  }

  return (
    <div ref={viewportRef} className="relative h-full w-full overflow-auto bg-accent/30">
      {rendering && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <div className="flex items-center gap-2 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>{t("knowledge.readerLoading")}</span>
          </div>
        </div>
      )}
      {failed ? (
        <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center text-xs text-muted">
          <AlertCircle className="h-4 w-4 opacity-70" />
          <p>{t("knowledge.readerDocxError")}</p>
        </div>
      ) : (
        // docx-preview 自己铺页面（灰底上的白纸），渲染后再按视口缩放贴合
        <div ref={containerRef} className="min-w-full py-4" />
      )}
    </div>
  );
}
