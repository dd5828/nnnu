"use client";

/**
 * 二进制 Office（pptx 等）的预览：没有可靠的浏览器渲染器，退回解析文本。
 *
 * 这里拉的是 /content（解析结果，JSON）而不是原件——二进制 zip 没法当文本读，
 * 而 markitdown 抽出来的这段正是模型读到的内容。文本按纯文本展示、不做
 * Markdown 渲染（markitdown 的输出是给模型看的，排版不值得再解释一遍）。
 *
 * 结果自带 url 键：对不上就是 loading 态，不用在 effect 里重置状态。
 */

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useI18n } from "@/hooks/useI18n";
import type { KbDocContent } from "@/types/api";
import { PreviewFailure, PreviewSpinner } from "./PreviewStatus";

interface ContentLoad {
  url: string;
  text: string | null;
  failed: boolean;
}

export default function OfficeTextPreview({ contentUrl }: { contentUrl: string }) {
  const { t } = useI18n();
  const [loaded, setLoaded] = useState<ContentLoad | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await apiFetch<KbDocContent>(contentUrl);
        if (alive) {
          setLoaded({ url: contentUrl, text: data.text, failed: false });
        }
      } catch {
        // 含 409 not_parsed（正在解析或解析失败）——统一按加载失败展示
        if (alive) {
          setLoaded({ url: contentUrl, text: null, failed: true });
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [contentUrl]);

  const current = loaded && loaded.url === contentUrl ? loaded : null;

  if (current?.failed) {
    return <PreviewFailure code="load" />;
  }
  if (!current || current.text === null) {
    return <PreviewSpinner />;
  }

  return (
    <div className="flex h-full flex-col">
      <p className="shrink-0 border-b border-border bg-accent/40 px-4 py-2 text-[11px] text-muted">
        {t("knowledge.readerOfficeTextHint")}
      </p>
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        <pre className="whitespace-pre-wrap break-words font-sans text-sm">{current.text}</pre>
      </div>
    </div>
  );
}
