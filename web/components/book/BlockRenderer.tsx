"use client";

/** 块渲染器：按 type 分派十二类块 + 悬浮工具条 + 就地编辑。
 *
 * - text / callout / deep_dive / note 与 figure / interactive_html / animation 都是
 *   正文 Markdown（后三者的围栏由聊天渲染层 Markdown.tsx 自动分发 RenderViewer /
 *   ArtifactCard——零新渲染代码）；quiz / flashcard / timeline / code / concept_graph
 *   各有专用组件；
 * - code 块拉着 Prism（重库）：dynamic import 到这里，ssr:false（照 knowledge 的
 *   TextPreview 手法）；
 * - 没生成完的块（pending/compiling/error）给占位与重试，不渲染 payload。
 */

import type { ReactNode } from "react";
import dynamic from "next/dynamic";
import { Loader2, RotateCcw } from "lucide-react";
import Markdown from "@/components/chat/Markdown";
import BlockToolbar from "@/components/book/BlockToolbar";
import ConceptGraphBlock from "@/components/book/blocks/ConceptGraphBlock";
import FlashcardBlock from "@/components/book/blocks/FlashcardBlock";
import QuizBlock from "@/components/book/blocks/QuizBlock";
import TimelineBlock from "@/components/book/blocks/TimelineBlock";
import { useI18n } from "@/hooks/useI18n";
import { calloutVariant, deepDiveSources, markdownOf } from "@/lib/book";
import type { BlockType, BookAttempt, BookBlock } from "@/types/api";

// Prism 重库跟着代码块走（静态引入会拽进主包，照 KnowledgeReader 的懒加载边界）
const CodeBlock = dynamic(() => import("@/components/book/blocks/CodeBlock"), {
  ssr: false,
  loading: () => <p className="text-xs text-muted">…</p>,
});

export interface BlockActions {
  index: number;
  total: number;
  /** 有编辑请求在飞（全局忙，所有工具条按钮禁掉）。 */
  busy: boolean;
  /** 忙的就是这个块（转圈放它身上）。 */
  busyHere: boolean;
  editing: boolean;
  draft: string;
  canEdit: boolean;
  onMove: (direction: "up" | "down") => void;
  onInsert: (type: BlockType) => void;
  onRegenerate: () => void;
  onRetype: (type: BlockType) => void;
  onDelete: () => void;
  onStartEdit: () => void;
  onChangeDraft: (text: string) => void;
  onSave: () => void;
  onCancelEdit: () => void;
}

interface Props {
  block: BookBlock;
  bookId: string;
  pageId: string;
  attempt: BookAttempt | null;
  actions: BlockActions;
}

const CALLOUT_CLASS: Record<string, string> = {
  info: "border-primary/30 bg-primary/5",
  tip: "border-success/40 bg-success/5",
  warn: "border-danger/40 bg-danger/5",
  important: "border-primary/50 bg-primary/10",
};

/** 自己不带壳的交互块（外壳由 section 统一给：圆角边框 + 内边距）。 */
const PADDED_BLOCKS = new Set(["quiz", "flashcard", "timeline", "concept_graph"]);

export default function BlockRenderer({ block, bookId, pageId, attempt, actions }: Props) {
  const { t } = useI18n();
  const markdown = markdownOf(block.payload);

  const sectionClass = PADDED_BLOCKS.has(block.type)
    ? "group relative rounded-xl border border-border bg-surface/40 p-4"
    : block.type === "note"
      ? "group relative rounded-xl border border-dashed border-border p-4"
      : "group relative";

  let body: ReactNode;
  if (block.status === "compiling") {
    body = (
      <p className="inline-flex items-center gap-2 text-xs text-muted">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t("book.blockCompilingHint")}
      </p>
    );
  } else if (block.status === "error") {
    body = (
      <div className="space-y-2">
        <p data-testid="bk-block-error" className="text-xs text-danger">
          {t("book.blockErrorHint", { message: block.error || "?" })}
        </p>
        <button
          type="button"
          disabled={actions.busy}
          onClick={actions.onRegenerate}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          {t("book.regenerateBlock")}
        </button>
      </div>
    );
  } else if (block.status === "pending") {
    body = (
      <div className="space-y-2">
        <p className="text-xs text-muted">{t("book.blockPendingHint")}</p>
        {!actions.busyHere && (
          <button
            type="button"
            disabled={actions.busy}
            onClick={actions.onRegenerate}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {t("book.blockGenerate")}
          </button>
        )}
      </div>
    );
  } else if (actions.editing) {
    body = (
      <div className="space-y-2">
        <textarea
          data-testid="bk-block-editor"
          value={actions.draft}
          onChange={(event) => actions.onChangeDraft(event.target.value)}
          placeholder={t("book.editPlaceholder")}
          spellCheck={false}
          className="min-h-40 w-full resize-y rounded-lg border border-border bg-transparent p-2.5 font-mono text-xs leading-relaxed outline-none focus:border-primary/50"
        />
        <div className="flex items-center gap-2">
          <button
            type="button"
            data-testid="bk-block-save"
            disabled={actions.busy}
            onClick={actions.onSave}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
          >
            {actions.busyHere && <Loader2 className="h-3 w-3 animate-spin" />}
            {t("common.save")}
          </button>
          <button
            type="button"
            data-testid="bk-block-cancel"
            disabled={actions.busy}
            onClick={actions.onCancelEdit}
            className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
          >
            {t("common.cancel")}
          </button>
        </div>
      </div>
    );
  } else if (block.type === "quiz") {
    body = <QuizBlock block={block} bookId={bookId} pageId={pageId} attempt={attempt} />;
  } else if (block.type === "flashcard") {
    body = <FlashcardBlock block={block} />;
  } else if (block.type === "timeline") {
    body = <TimelineBlock block={block} />;
  } else if (block.type === "code") {
    body = <CodeBlock block={block} />;
  } else if (block.type === "concept_graph") {
    body = <ConceptGraphBlock block={block} />;
  } else if (block.type === "callout") {
    body = (
      <div
        className={`rounded-lg border px-3.5 py-2.5 ${
          CALLOUT_CLASS[calloutVariant(block.payload)]
        }`}
      >
        <Markdown text={markdown} />
      </div>
    );
  } else if (block.type === "deep_dive") {
    const sources = deepDiveSources(block.payload);
    body = (
      <div className="rounded-xl border border-border bg-surface/50 p-4">
        <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
          {t("book.blockType_deep_dive")}
        </p>
        <div className="mt-2">
          <Markdown text={markdown} />
        </div>
        {sources.length > 0 && (
          <ul className="mt-3 space-y-0.5 border-t border-border pt-2">
            {sources.map((source, index) => (
              <li key={index} className="text-xs text-muted">
                {source}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  } else if (block.type === "note" && !markdown.trim()) {
    body = <p className="text-xs text-muted">{t("book.noteEmpty")}</p>;
  } else {
    body = <Markdown text={markdown} />;
  }

  return (
    <section
      data-testid="bk-block"
      data-block-id={block.id}
      data-type={block.type}
      data-status={block.status}
      className={sectionClass}
    >
      <BlockToolbar block={block} actions={actions} />
      {body}
    </section>
  );
}
