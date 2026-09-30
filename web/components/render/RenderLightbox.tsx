"use client";

/** 渲染块全屏浮层（§7.7）：esc / 点背景 / 点关闭都能退。
 *
 * 用 createPortal 挂到 body：完整屏的定位不该被消息列表的 overflow/transform 裁剪。
 */

import { useEffect } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";

export default function RenderLightbox({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const { t } = useI18n();

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  if (typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <div
      data-testid="render-lightbox"
      className="fixed inset-0 z-[100] flex flex-col bg-background/95 backdrop-blur-sm"
      onClick={onClose}
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <span className="text-xs text-muted">{title}</span>
        <button
          type="button"
          data-testid="render-lightbox-close"
          onClick={onClose}
          aria-label={t("render.close")}
          className="rounded-full p-1 text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div
        className="flex flex-1 items-center justify-center overflow-auto p-4"
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>,
    document.body
  );
}
