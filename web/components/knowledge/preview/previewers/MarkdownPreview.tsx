"use client";

/**
 * Markdown 预览：拉原件文本，交给聊天那套 Markdown 渲染器（GFM + KaTeX +
 * .markdown-body 样式全免费获得，不在知识库这边重复造）。
 */

import Markdown from "@/components/chat/Markdown";
import { useTextSource } from "../useTextSource";
import { PreviewFailure, PreviewSpinner } from "./PreviewStatus";

export default function MarkdownPreview({ url }: { url: string }) {
  const src = useTextSource(url);

  if (src.kind === "loading") {
    return <PreviewSpinner />;
  }
  if (src.kind === "error") {
    return <PreviewFailure code={src.code} />;
  }
  return (
    <div className="h-full overflow-y-auto px-4 py-3">
      <Markdown text={src.text} />
    </div>
  );
}
