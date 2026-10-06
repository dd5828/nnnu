/** 对话引用（§7.1 一次性引用注入 / §7.21「+」引用菜单）的类型与编解码。
 *
 * 三种形状各管一段路，别混：
 * - `PendingRef`：输入区「+」菜单挑出来的**待发**引用（本地态，带展示用 label）；
 * - wire 形状（`toWireRefs`）：随 WS 载荷发的 `refs`，后端 `TurnRefs` 只认 id
 *   （字段必须在服务端显式声明，Pydantic `extra="ignore"` 会静默丢弃没声明的键，
 *   test_refs.py 有防回归用例）；
 * - `RefEntry`：消息 metadata 里的**发送时快照**（服务端 `_resolve_*` 写入），
 *   刷新后消息上的 chip 由它还原；`resolved=false` = 发送后目标被删，chip 渲染
 *   失效态、不跳转。
 *
 * 落点计算（entry → 深链）不在这儿：在 `citations.ts:refTarget`，与助手引文同一处，
 * 单测也在那边。本文件全是纯函数，单测见 tests/refs.test.ts。
 */

/** 消息 metadata 里的引用快照条目（对应服务端 snapshot 的形状，逐字段对齐）。 */
export type RefEntry =
  | {
      kind: "notebook_record";
      notebook_id: string;
      record_id: string;
      label: string;
      resolved: boolean;
    }
  | { kind: "question"; question_id: string; label: string; resolved: boolean };

/** 待发引用（「+」菜单选出来的）：id 齐、label 给 chip 看。 */
export type PendingRef =
  | { kind: "notebook_record"; notebook_id: string; record_id: string; label: string }
  | { kind: "question"; question_id: string; label: string };

/** 会话消息 metadata（后端 `Message.metadata` 是自由 dict，目前只有 refs 一种键）。 */
export interface MessageMetadata {
  refs?: RefEntry[];
}

/** 稳定空数组：传给 memo 组件时不破坏引用相等（`?? []` 每次都是新数组，会白重渲）。 */
export const EMPTY_REFS: RefEntry[] = [];

/** 去重键：同类看 id、异类不同键（记录 nbr-1 与题目 q-1 永不相撞）。 */
export function refKey(ref: PendingRef | RefEntry): string {
  return ref.kind === "notebook_record"
    ? `notebook_record:${ref.record_id}`
    : `question:${ref.question_id}`;
}

export interface WireRefs {
  notebooks: { notebook_id: string; record_id: string }[];
  questions: { question_id: string }[];
}

/** 待发引用 → WS 载荷的 `refs`：按类分组、只带 id（label 是本地展示用，不发）。 */
export function toWireRefs(refs: PendingRef[]): WireRefs {
  const wire: WireRefs = { notebooks: [], questions: [] };
  for (const ref of refs) {
    if (ref.kind === "notebook_record") {
      wire.notebooks.push({ notebook_id: ref.notebook_id, record_id: ref.record_id });
    } else {
      wire.questions.push({ question_id: ref.question_id });
    }
  }
  return wire;
}

/** 待发引用 → 乐观消息的 metadata 快照：本地选的都当已解析（选的时候刚从列表里来）。 */
export function toSnapshotEntries(refs: PendingRef[]): RefEntry[] {
  return refs.map((ref): RefEntry => ({ ...ref, resolved: true }));
}

/** 服务器 metadata（自由 dict）→ 规范 `RefEntry[]`：坏条目宽容跳过
 *  （对齐服务端 `_refs_from_metadata` 的宽容语义，老消息/手改库都不炸）。 */
export function normalizeRefs(metadata: unknown): RefEntry[] {
  if (typeof metadata !== "object" || metadata === null) {
    return [];
  }
  const raw = (metadata as { refs?: unknown }).refs;
  if (!Array.isArray(raw)) {
    return [];
  }
  const entries: RefEntry[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) {
      continue;
    }
    const entry = item as Record<string, unknown>;
    const label = typeof entry.label === "string" ? entry.label : "";
    const resolved = entry.resolved === true;
    if (
      entry.kind === "notebook_record" &&
      typeof entry.record_id === "string" &&
      entry.record_id !== ""
    ) {
      entries.push({
        kind: "notebook_record",
        notebook_id: typeof entry.notebook_id === "string" ? entry.notebook_id : "",
        record_id: entry.record_id,
        label,
        resolved,
      });
    } else if (
      entry.kind === "question" &&
      typeof entry.question_id === "string" &&
      entry.question_id !== ""
    ) {
      entries.push({ kind: "question", question_id: entry.question_id, label, resolved });
    }
  }
  return entries;
}

/** 题干 → 单行 chip 标签（口径对齐服务端 `_question_label`：折空白、截 60 字）。 */
export function questionRefLabel(stem: string): string {
  const line = stem.replace(/\s+/g, " ").trim();
  return line.length > 60 ? `${line.slice(0, 60)}…` : line;
}
