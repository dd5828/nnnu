"use client";

/** 兜底预览：认不出的类型只给个说明 + 下载出口。 */

import { Download, FileQuestion } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";

export default function FallbackPreview({ filename, url }: { filename: string; url: string }) {
  const { t } = useI18n();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent text-muted">
        <FileQuestion className="h-5 w-5" />
      </div>
      <div>
        <p className="text-sm font-medium">{filename}</p>
        <p className="mt-1 text-xs text-muted">{t("knowledge.readerFallback")}</p>
      </div>
      <a
        href={url}
        download={filename}
        className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent"
      >
        <Download className="h-3.5 w-3.5" />
        {t("knowledge.readerDownload")}
      </a>
    </div>
  );
}
