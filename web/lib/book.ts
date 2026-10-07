/** Book 活书纯函数（§7.14）：枚举对齐后端 + 看板/估算/进度的展示口径。
 *
 * 零 React、零请求：估算与进度**不在前端镜像公式**（后端 GET 现算返回），
 * 这里只做「把数字翻译成人话」和数组编辑这类确定性变换，vitest 直接锁。
 */

import type {
  BlockStatus,
  BlockType,
  BookChapter,
  BookHealth,
  BookPageMeta,
  BookSourceRef,
  BookSources,
  BookSpine,
  BookStatus,
} from "@/types/api";

/** 编译中才轮询：1500ms 一次，编译看板上肉眼像连续（照知识库 800ms 的手法）。 */
export const BOOK_POLL_MS = 1500;

/** 五个书状态（键与顺序跟后端 book/models.BOOK_STATUSES 同一张表）。 */
export const BOOK_STATUSES = ["draft", "compiling", "paused", "ready", "error"] as const;

/** 块状态（同上，对齐 BLOCK_STATUSES）。 */
export const BLOCK_STATUSES = ["pending", "compiling", "done", "error"] as const;

/** 十二类块（对齐 BLOCK_TYPES；顺序即「换类型」菜单的顺序）。 */
export const BLOCK_TYPES = [
  "text",
  "callout",
  "quiz",
  "flashcard",
  "timeline",
  "code",
  "figure",
  "interactive_html",
  "animation",
  "concept_graph",
  "deep_dive",
  "note",
] as const satisfies readonly BlockType[];

export function isBookBusy(status: BookStatus): boolean {
  return status === "compiling";
}

/** 状态徽标配色（border/text 两件套，照 knowledge/status.tsx 的 chip 手法；
 *  不引新的主题色：暂停只是稍弱的蓝。 */
const STATUS_CLASS: Record<BookStatus, string> = {
  draft: "border-border text-muted",
  compiling: "border-primary/40 text-primary",
  paused: "border-primary/30 text-primary",
  ready: "border-success/40 text-success",
  error: "border-danger/40 text-danger",
};

export function bookStatusClass(status: BookStatus): string {
  return STATUS_CLASS[status] ?? STATUS_CLASS.error;
}

export function bookStatusKey(status: BookStatus): string {
  return `book.status_${status}`;
}

/** 块状态配色（编译看板的小方块用）。 */
const BLOCK_STATUS_CLASS: Record<BlockStatus, string> = {
  pending: "bg-accent",
  compiling: "bg-primary animate-pulse",
  done: "bg-success",
  error: "bg-danger",
};

export function blockStatusClass(status: BlockStatus): string {
  return BLOCK_STATUS_CLASS[status] ?? BLOCK_STATUS_CLASS.error;
}

export function blockStatusKey(status: BlockStatus): string {
  return `book.blockStatus_${status}`;
}

export function blockTypeKey(type: string): string {
  return `book.blockType_${type}`;
}

/** 百分比：总数 0 时给 0（不炸除）。 */
export function percentOf(done: number, total: number): number {
  return total > 0 ? Math.round((done / total) * 100) : 0;
}

export interface CompileStats {
  total: number;
  done: number;
  error: number;
  running: number;
  pending: number;
  percent: number;
}

/** 整书编译读数：所有页的块摊平计数（看板总进度条与文案用）。 */
export function compileStats(pages: BookPageMeta[]): CompileStats {
  let total = 0;
  let done = 0;
  let error = 0;
  let running = 0;
  let pending = 0;
  for (const page of pages) {
    for (const block of page.blocks) {
      total += 1;
      if (block.status === "done") {
        done += 1;
      } else if (block.status === "error") {
        error += 1;
      } else if (block.status === "compiling") {
        running += 1;
      } else {
        pending += 1;
      }
    }
  }
  return { total, done, error, running, pending, percent: percentOf(done, total) };
}

export interface ChapterRow {
  chapter: BookChapter;
  page: BookPageMeta | null;
}

/** 目录行：spine 章节 × 页 meta（按 chapter_key 配；理论上不会缺，缺了给 null）。 */
export function chapterRows(spine: BookSpine, pages: BookPageMeta[]): ChapterRow[] {
  const byKey = new Map(pages.map((page) => [page.chapter_key, page]));
  return spine.chapters.map((chapter) => ({
    chapter,
    page: byKey.get(chapter.key) ?? null,
  }));
}

/** 漂移 → 受影响的章：章的 source_refs 里引到的源 ref 命中漂移条目才算
 *  （书没引用它，重编也无从下手；命中不到就回空数组）。 */
export function driftChapters(spine: BookSpine, drift: BookHealth["drift"]): BookChapter[] {
  const refs = new Set(drift.map((item) => item.ref));
  if (refs.size === 0) {
    return [];
  }
  return spine.chapters.filter((chapter) =>
    chapter.source_refs.some((source) => refs.has(source.ref))
  );
}

/** 章的第一页（开始阅读 / 目录跳转用）；没有页给 null。 */
export function pageOfChapter(pages: BookPageMeta[], chapterKey: string): BookPageMeta | null {
  return pages.find((page) => page.chapter_key === chapterKey) ?? null;
}

// ---- 目录编辑（SpineEditor 用；全是不可变变换，原数组不动） ----

export function moveChapter(
  chapters: BookChapter[],
  index: number,
  direction: "up" | "down"
): BookChapter[] {
  const target = direction === "up" ? index - 1 : index + 1;
  if (index < 0 || index >= chapters.length || target < 0 || target >= chapters.length) {
    return chapters;
  }
  const next = [...chapters];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function removeChapter(chapters: BookChapter[], key: string): BookChapter[] {
  return chapters.filter((chapter) => chapter.key !== key);
}

export function renameChapter(chapters: BookChapter[], key: string, title: string): BookChapter[] {
  return chapters.map((chapter) => (chapter.key === key ? { ...chapter, title } : chapter));
}

/** 块计划摊成一行「3 文字 · 1 测验」那样的后缀文本（章节行用）。 */
export function planTypeCounts(blocks: BookChapter["blocks_plan"]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const plan of blocks) {
    counts.set(plan.type, (counts.get(plan.type) ?? 0) + 1);
  }
  return counts;
}

// ---- 素材选择摘要（书库卡片/控制台眉注用） ----

export interface SourceSummary {
  kbs: number;
  notebooks: number;
  sessions: number;
  /** 题库口径：none 是没选。 */
  questions: "none" | "all" | "wrong" | "ids";
  questionIds: number;
}

export function sourceSummary(sources: BookSources | null | undefined): SourceSummary {
  const kbs = sources?.kbs ?? [];
  const notebooks = sources?.notebooks ?? [];
  const sessions = sources?.sessions ?? [];
  const questions = sources?.questions ?? null;
  return {
    kbs: kbs.length,
    notebooks: notebooks.length,
    sessions: sessions.length,
    questions: questions ? questions.filter : "none",
    questionIds: questions?.ids.length ?? 0,
  };
}

/** 来源引用短标签（章的 source_refs 展示）。 */
export function sourceRefText(ref: BookSourceRef): string {
  return ref.label || ref.ref;
}

// ---- 估算与数字格式化 ----

/** token 数：四位数起进到 k（估算本来就是粗数，没必要逐位）。 */
export function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "0";
  }
  if (value < 1000) {
    return String(Math.round(value));
  }
  if (value < 10_000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return `${Math.round(value / 1000)}k`;
}

/** 费用：价格表口径（美元级），小于一分给三位防显示成 0.00。 */
export function formatCost(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "$0";
  }
  return value < 0.01 ? `$${value.toFixed(3)}` : `$${value.toFixed(2)}`;
}

/** 秒数拆成分/秒（文案在 locales：book.duration）。 */
export function splitDuration(seconds: number): { minutes: number; seconds: number } {
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds) : 0;
  return { minutes: Math.floor(total / 60), seconds: total % 60 };
}

// ---- 块 payload 解析（阅读器用；形状在编译期/编辑时已校验，这里只做宽容取值） ----

/** 有 markdown 正文的块（编辑正文、直接喂 <Markdown> 的都在这一类里）。 */
export const MARKDOWN_BLOCK_TYPES: readonly string[] = [
  "text",
  "callout",
  "deep_dive",
  "note",
  "figure",
  "interactive_html",
  "animation",
];

export function isMarkdownBlock(type: string): boolean {
  return MARKDOWN_BLOCK_TYPES.includes(type);
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** payload.markdown（fence 家族/figure 的整段正文就是这个）。 */
export function markdownOf(payload: Record<string, unknown>): string {
  return textOf(payload.markdown);
}

export function calloutVariant(payload: Record<string, unknown>): string {
  const variant = textOf(payload.variant);
  return ["info", "tip", "warn", "important"].includes(variant) ? variant : "info";
}

export function deepDiveSources(payload: Record<string, unknown>): string[] {
  const raw = payload.sources;
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
}

export interface QuizView {
  stem: string;
  options: { key: string; text: string }[];
  explanation: string;
}

/** quiz payload → 视图。**不取 answer_key**：作答前界面不该知道答案。 */
export function parseQuiz(payload: Record<string, unknown>): QuizView {
  const raw = Array.isArray(payload.options) ? payload.options : [];
  const options = raw.flatMap((item) => {
    if (typeof item !== "object" || item === null) {
      return [];
    }
    const record = item as Record<string, unknown>;
    const key = textOf(record.key).toUpperCase();
    const text = textOf(record.text);
    return key && text ? [{ key, text }] : [];
  });
  return { stem: textOf(payload.stem), options, explanation: textOf(payload.explanation) };
}

export interface FlashcardView {
  cards: { front: string; back: string }[];
}

export function parseFlashcards(payload: Record<string, unknown>): FlashcardView {
  const raw = Array.isArray(payload.cards) ? payload.cards : [];
  const cards = raw.flatMap((item) => {
    if (typeof item !== "object" || item === null) {
      return [];
    }
    const record = item as Record<string, unknown>;
    const front = textOf(record.front);
    const back = textOf(record.back);
    return front && back ? [{ front, back }] : [];
  });
  return { cards };
}

export interface TimelineView {
  events: { when: string; title: string; detail: string }[];
}

export function parseTimeline(payload: Record<string, unknown>): TimelineView {
  const raw = Array.isArray(payload.events) ? payload.events : [];
  const events = raw.flatMap((item) => {
    if (typeof item !== "object" || item === null) {
      return [];
    }
    const record = item as Record<string, unknown>;
    const when = textOf(record.when);
    const title = textOf(record.title);
    return when && title ? [{ when, title, detail: textOf(record.detail) }] : [];
  });
  return { events };
}

export interface CodeView {
  language: string;
  code: string;
  explanation: string;
}

export function parseCode(payload: Record<string, unknown>): CodeView {
  return {
    language: textOf(payload.language) || "text",
    code: textOf(payload.code),
    explanation: textOf(payload.explanation),
  };
}

/** 页里还有没生成完的块（轮询开关：animation 再生是唯一的 202 路径）。 */
export function hasPendingBlocks(blocks: { status: BlockStatus }[]): boolean {
  return blocks.some((block) => block.status !== "done" && block.status !== "error");
}
