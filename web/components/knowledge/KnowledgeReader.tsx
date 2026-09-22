"use client";

/**
 * 阅读器（§7.9）：PDF 用 iframe 指到原件的 `#page=N` 页锚，文本类拉 /content 铺 <pre>。
 *
 * 页锚支持是浏览器行为：Chromium/Edge 的内置 PDF 查看器认 `#page=N`，Firefox 不认
 * （所以面板上留了一句提示）。切换页码时给 iframe 换 key，强制重新导航——不然
 * 光改 fragment 不会让查看器跳页。
 */

import { useEffect, useState } from "react";
import { ExternalLink, Loader2, X } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useI18n } from "@/hooks/useI18n";
import type { KbDoc, KbDocContent } from "@/types/api";

interface KnowledgeReaderProps {
  kbId: string;
  doc: KbDoc;
  page: number | null;
  onClose: () => void;
}

/** 拉回来的解析文本：key 是文档地址——换了文档，旧结果自然不算数（不用手动清）。 */
interface LoadedContent {
  key: string;
  content: KbDocContent | null;
  error: string | null;
}

export default function KnowledgeReader({ kbId, doc, page, onClose }: KnowledgeReaderProps) {
  const { t } = useI18n();
  const isPdf = doc.mime.includes("pdf") || doc.filename.toLowerCase().endsWith(".pdf");
  const docBase = `/api/v1/kbs/${kbId}/docs/${doc.doc_id}`;
  const [loaded, setLoaded] = useState<LoadedContent | null>(null);

  useEffect(() => {
    if (isPdf) {
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const data = await apiFetch<KbDocContent>(`${docBase}/content`);
        if (alive) {
          setLoaded({ key: docBase, content: data, error: null });
        }
      } catch (loadError) {
        if (alive) {
          setLoaded({ key: docBase, content: null, error: String(loadError) });
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [docBase, isPdf]);

  const current = loaded?.key === docBase ? loaded : null;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

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
          <a
            href={`${docBase}/file`}
            target="_blank"
            rel="noreferrer"
            title={t("knowledge.openOriginal")}
            className="ml-auto shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent"
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

        {isPdf ? (
          <>
            <iframe
              key={`${doc.doc_id}-${page ?? 0}`}
              title={doc.filename}
              src={page !== null ? `${docBase}/file#page=${page}` : `${docBase}/file`}
              className="min-h-0 flex-1 bg-background"
            />
            <p className="border-t border-border px-4 py-1.5 text-[11px] text-muted">
              {t("knowledge.readerPdfHint")}
            </p>
          </>
        ) : (
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            {current?.error ? (
              <p className="text-sm text-danger">{current.error}</p>
            ) : !current?.content ? (
              <Loader2 className="h-4 w-4 animate-spin text-muted" />
            ) : (
              <pre className="whitespace-pre-wrap break-words font-sans text-sm">
                {current.content.text}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
