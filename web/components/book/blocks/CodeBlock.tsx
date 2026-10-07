"use client";

/** 代码块：Prism 高亮（重库，**必须由注意包体的地方 dynamic import**——照
 *  knowledge 的 TextPreview 手法：Prism 静态 import 在本文件里，懒加载边界放在
 *  BlockRenderer 的 next/dynamic，ssr:false）＋ 复制按钮 + 讲解正文。 */

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import oneDark from "react-syntax-highlighter/dist/esm/styles/prism/one-dark";
import Markdown from "@/components/chat/Markdown";
import { useI18n } from "@/hooks/useI18n";
import { parseCode } from "@/lib/book";
import type { BookBlock } from "@/types/api";

const MONOSPACE =
  'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace';

/** 语言名归一：LLM 偶尔给 "Python 3" 这种，Prism 只认纯标识符。 */
function normalizeLang(language: string): string {
  const clean = language
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9+#-]/g, "");
  return clean || "text";
}

export default function CodeBlock({ block }: { block: BookBlock }) {
  const { t } = useI18n();
  const view = parseCode(block.payload);
  const [copied, setCopied] = useState(false);
  const lang = normalizeLang(view.language);

  const copy = () => {
    void navigator.clipboard?.writeText(view.code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="space-y-2" data-testid="bk-code" data-block-id={block.id}>
      <div className="overflow-hidden rounded-xl border border-border">
        <div className="flex items-center justify-between border-b border-border bg-accent/40 px-3 py-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wider text-muted">
            {lang}
          </span>
          <button
            type="button"
            data-testid="bk-code-copy"
            onClick={copy}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? t("book.codeCopied") : t("book.codeCopy")}
          </button>
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
          {view.code}
        </SyntaxHighlighter>
      </div>
      {view.explanation && (
        <div className="text-sm">
          <Markdown text={view.explanation} />
        </div>
      )}
    </div>
  );
}
