"use client";

/**
 * 阅读器（§7.9）：按文件类型分发预览。
 *
 * - PDF  → 浏览器原生 iframe，`#page=N` 引用跳页（切页时换 key 强制重导航）；
 * - docx/xlsx → 原件字节交给懒加载的渲染器（docx-preview / exceljs）；
 * - md / 代码 / 文本 → 拉原件文本（草稿、代码要高亮原文，不用 /content 的规范化副本）；
 * - pptx 等 → /content 的解析文本（二进制没法当文本读）；
 * - 其余 → 兜底下载。
 *
 * 三个重库只在懒加载分块里出现，别在本文件静态引它们。
 */

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { Check, Copy, Download, ExternalLink, X } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import type { KbDoc } from "@/types/api";
import { previewKindFor } from "./preview/previewerFor";
import FallbackPreview from "./preview/previewers/FallbackPreview";
import MarkdownPreview from "./preview/previewers/MarkdownPreview";
import OfficeTextPreview from "./preview/previewers/OfficeTextPreview";
import { PreviewSpinner } from "./preview/previewers/PreviewStatus";

// 重库跟着这三个走（docx-preview / exceljs / react-syntax-highlighter），
// 静态引入会把它们拽进主包。
const DocxPreview = dynamic(() => import("./preview/previewers/DocxPreview"), {
  ssr: false,
  loading: PreviewSpinner,
});
const XlsxPreview = dynamic(() => import("./preview/previewers/XlsxPreview"), {
  ssr: false,
  loading: PreviewSpinner,
});
const TextPreview = dynamic(() => import("./preview/previewers/TextPreview"), {
  ssr: false,
  loading: PreviewSpinner,
});

interface KnowledgeReaderProps {
  kbId: string;
  doc: KbDoc;
  page: number | null;
  onClose: () => void;
}

export default function KnowledgeReader({ kbId, doc, page, onClose }: KnowledgeReaderProps) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const docBase = `/api/v1/kbs/${kbId}/docs/${doc.doc_id}`;
  const fileUrl = `${docBase}/file`;
  const kind = previewKindFor({ filename: doc.filename, mimeType: doc.mime });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const copyLink = async () => {
    const link = `${location.origin}/knowledge/${kbId}?doc=${doc.doc_id}${
      page !== null ? `&page=${page}` : ""
    }`;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板被拒就算了，头部还有原文/下载两个出口
    }
  };

  return (
    <div
      data-testid="reader"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex h-full max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
          <span className="truncate text-sm font-medium">{doc.filename}</span>
          {page !== null && (
            <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
              {t("chat.page", { n: String(page) })}
            </span>
          )}
          <button
            type="button"
            data-testid="reader-copy-link"
            onClick={() => void copyLink()}
            title={copied ? t("knowledge.readerCopied") : t("knowledge.readerCopyLink")}
            className="ml-auto shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent"
          >
            {copied ? <Check className="h-4 w-4 text-success" /> : <Copy className="h-4 w-4" />}
          </button>
          <a
            data-testid="reader-download"
            href={fileUrl}
            download={doc.filename}
            title={t("knowledge.readerDownload")}
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent"
          >
            <Download className="h-4 w-4" />
          </a>
          <a
            href={fileUrl}
            target="_blank"
            rel="noreferrer"
            title={t("knowledge.openOriginal")}
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent"
          >
            <ExternalLink className="h-4 w-4" />
          </a>
          <button
            type="button"
            onClick={onClose}
            title={t("common.close")}
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {kind === "pdf" ? (
          <>
            <iframe
              key={`${doc.doc_id}-${page ?? 0}`}
              title={doc.filename}
              src={page !== null ? `${fileUrl}#page=${page}` : fileUrl}
              className="min-h-0 flex-1 bg-background"
            />
            <p className="border-t border-border px-4 py-1.5 text-[11px] text-muted">
              {t("knowledge.readerPdfHint")}
            </p>
          </>
        ) : (
          // 每个渲染器自带滚动容器（docx 的缩放贴合要量自己的视口宽度）
          <div className="min-h-0 flex-1">
            {kind === "docx" && <DocxPreview url={fileUrl} />}
            {kind === "xlsx" && <XlsxPreview url={fileUrl} />}
            {kind === "markdown" && <MarkdownPreview url={fileUrl} />}
            {kind === "code" && <TextPreview url={fileUrl} filename={doc.filename} />}
            {kind === "text" && <TextPreview url={fileUrl} filename={doc.filename} />}
            {kind === "office-text" && <OfficeTextPreview contentUrl={`${docBase}/content`} />}
            {kind === "fallback" && <FallbackPreview filename={doc.filename} url={fileUrl} />}
          </div>
        )}
      </div>
    </div>
  );
}
