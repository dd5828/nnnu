"use client";

/**
 * 纯文本 / 代码预览：拉原件文本，代码文件按扩展名高亮，其余按等宽纯文本铺。
 *
 * react-syntax-highlighter 是重库（整个 Prism 语言包），静态 import 在这里
 * 意味着它只随本文件的懒加载分块走——KnowledgeReader 里绝不能直接引它。
 * 不开行号（知识库没这设置项），所以也就不需要 DeepTutor 那个 flex 绕行。
 */

import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import oneDark from "react-syntax-highlighter/dist/esm/styles/prism/one-dark";
import { langForFilename } from "../langForFilename";
import { useTextSource } from "../useTextSource";
import { PreviewFailure, PreviewSpinner } from "./PreviewStatus";

const MONOSPACE =
  'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace';

export default function TextPreview({ url, filename }: { url: string; filename: string }) {
  const src = useTextSource(url);

  if (src.kind === "loading") {
    return <PreviewSpinner />;
  }
  if (src.kind === "error") {
    return <PreviewFailure code={src.code} />;
  }

  const lang = langForFilename(filename);
  if (!lang) {
    return (
      <div className="h-full overflow-auto px-4 py-3">
        <pre className="whitespace-pre-wrap break-words font-mono text-sm text-foreground">
          {src.text}
        </pre>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto px-4 py-3">
      <div className="overflow-hidden rounded-xl border border-border">
        <div className="border-b border-border bg-accent/40 px-3 py-1.5 text-[11px] font-medium uppercase tracking-wider text-muted">
          {lang}
        </div>
        <SyntaxHighlighter
          language={lang}
          style={oneDark}
          customStyle={{
            margin: 0,
            borderRadius: 0,
            padding: "0.75rem 1rem",
            fontSize: "0.875rem",
            lineHeight: "1.7",
          }}
          codeTagProps={{ style: { fontFamily: MONOSPACE } }}
        >
          {src.text}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}
