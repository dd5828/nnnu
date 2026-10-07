/** Co-Writer 的纯函数与常量（§7.13）。
 *
 * 全部是纯函数，交给 `tests/co-writer.test.ts` 盯（vitest 是 node 环境，
 * 只收 tests 目录下的 .test.ts，组件测试在本仓跑不起来）。
 *
 * 码点换算为什么必须有：后端 Python 的字符串下标是**码点**，JS 的字符串下标是
 * **UTF-16 码元**——BMP 字符两者一致，可 emoji、生僻字（代理对）在 JS 里算 2、
 * 在 Python 里算 1。不做换算，选区里含一个 emoji，整个 start/end 就偏了，
 * 服务端切片校验直接判 409。
 */

import type { CoWriterAction } from "@/types/api";

/** 自动保存防抖时长（ms）：停手两秒才 PATCH 一次（§7.13）。 */
export const AUTOSAVE_DELAY_MS = 2000;

/** 单次改写的选区上限（与后端 models.MAX_SELECTION_CHARS 同一张表）。 */
export const MAX_SELECTION_CHARS = 20_000;

/** 六个动作（顺序即工具条按钮顺序；键与后端 co_writer/models.ACTIONS 同一张表）。 */
export const CO_WRITER_ACTIONS: CoWriterAction[] = [
  "rewrite",
  "expand",
  "shorten",
  "translate",
  "tone",
  "free",
];

/** 动作键 → locale 文案键（locale coWriter.action_*）。 */
export function actionLabelKey(action: CoWriterAction): string {
  return `coWriter.action_${action}`;
}

// ---- localStorage 草稿镜像 ----

/** 草稿：还没 PATCH 出去的正文 + 记录时间（装载时发现服务端正文和它不一样就提示）。 */
export interface CoWriterDraft {
  content: string;
  savedAt: number;
}

const DRAFT_PREFIX = "nnnu.co-writer.draft.";

export function draftKey(docId: string): string {
  return `${DRAFT_PREFIX}${docId}`;
}

export function serializeDraft(draft: CoWriterDraft): string {
  return JSON.stringify(draft);
}

/** 读草稿：坏 JSON / 没有 content 一律当没有草稿（镜像坏了不能拦住打开文档）。 */
export function parseDraft(raw: string | null | undefined): CoWriterDraft | null {
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as { content?: unknown; savedAt?: unknown };
    if (typeof parsed.content !== "string") {
      return null;
    }
    return {
      content: parsed.content,
      savedAt: typeof parsed.savedAt === "number" ? parsed.savedAt : 0,
    };
  } catch {
    return null;
  }
}

export interface DraftResolution {
  content: string;
  /** true = 采用了草稿（和服务器正文不一样），界面要出「已恢复草稿」提示条。 */
  fromDraft: boolean;
}

/** 装载时用哪份正文：草稿和服务端一致（或没有草稿）用服务端，不同则默认用草稿。 */
export function resolveDraft(serverContent: string, draft: CoWriterDraft | null): DraftResolution {
  if (!draft || draft.content === serverContent) {
    return { content: serverContent, fromDraft: false };
  }
  return { content: draft.content, fromDraft: true };
}

// ---- 码点换算与写回 ----

/** JS 的 UTF-16 下标 → 码点下标（发给服务端的 start/end 要的是码点）。 */
export function codePointIndex(text: string, utf16Index: number): number {
  const end = Math.min(Math.max(utf16Index, 0), text.length);
  let codePoints = 0;
  for (let i = 0; i < end; ) {
    const code = text.codePointAt(i) ?? 0;
    i += code > 0xffff ? 2 : 1;
    codePoints += 1;
  }
  return codePoints;
}

/** 码点下标 → JS 的 UTF-16 下标（写回时切串用）。 */
export function utf16Index(text: string, codePointOffset: number): number {
  let codePoints = 0;
  let i = 0;
  while (i < text.length && codePoints < codePointOffset) {
    const code = text.codePointAt(i) ?? 0;
    i += code > 0xffff ? 2 : 1;
    codePoints += 1;
  }
  return i;
}

/** 码点计数的长度（选区上限按码点算，和后端口径一致）。 */
export function codePointLength(text: string): number {
  return [...text].length;
}

/** 本地写回一次改写：把 [start, end) 码点区间换成 edited（accept 成功后调）。 */
export function applyEdit(content: string, start: number, end: number, edited: string): string {
  return (
    content.slice(0, utf16Index(content, start)) + edited + content.slice(utf16Index(content, end))
  );
}
