/** 记忆工作台纯函数（§7.10）：标签键映射 / L1 行摘要 / 体量格式化。
 *
 * 全部无副作用、不碰 DOM——i18n 文案由调用方注入 t()，单测直接喂假 t。
 * 事件种类口径与后端 `services/memory/models.py` 的 EVENT_KINDS 对齐。
 */

import type { MemoryL1Row } from "@/types/api";

/** L1 事件种类（顺序即展示顺序；与后端 EVENT_KINDS 一致）。 */
export const EVENT_KINDS = [
  "user_message",
  "tool_call",
  "assistant_done",
  "ask_user",
  "cost",
] as const;

/** 事件种类的 i18n 键；未知种类回退展示原始名。 */
export function eventLabelKey(event: string): string {
  return `memory.event.${event}`;
}

export function surfaceLabelKey(surface: string): string {
  return `memory.surface.${surface}`;
}

export function l3DocLabelKey(doc: string): string {
  return `memory.doc.${doc}`;
}

export function layerLabelKey(layer: string): string {
  return `memory.layer.${layer}`;
}

export function originLabelKey(origin: string): string {
  return `memory.origin.${origin}`;
}

export function runStatusLabelKey(status: string): string {
  return `memory.run.${status}`;
}

/** 事件种类的徽标底色类（与层色无关，按事件性质分四档）。 */
export function eventBadgeClass(event: string): string {
  switch (event) {
    case "user_message":
      return "bg-graph-l1/15 text-graph-l1";
    case "tool_call":
      return "bg-graph-l2/15 text-graph-l2";
    case "assistant_done":
      return "bg-graph-l3/15 text-graph-l3";
    case "ask_user":
      return "bg-accent text-primary";
    case "cost":
      return "bg-accent text-muted";
    default:
      return "bg-accent text-muted";
  }
}

/**
 * L1 行 → 一行摘要（与后端 `graph._l1_text` 同口径，换成前端可渲染的文本）。
 * 未知字段一律防御性取值，宁可显示空串也不抛。
 */
export function l1Summary(row: MemoryL1Row): string {
  const data = (row.data ?? {}) as Record<string, unknown>;
  const text = typeof data.text === "string" ? data.text : "";
  switch (row.event) {
    case "user_message":
    case "assistant_done":
      return text;
    case "tool_call": {
      const name = String(data.tool_name ?? "");
      const detail = String(data.summary ?? data.args_preview ?? "");
      const tail = detail ? `${name}: ${detail}` : name;
      return data.ok === false ? `${tail} · ✗` : tail;
    }
    case "ask_user": {
      const question = String(data.question ?? "");
      const answer = String(data.answer ?? "");
      return data.answered && answer ? `${question} → ${answer}` : question;
    }
    case "cost": {
      const tokens = Number(data.tokens ?? 0);
      const cost = Number(data.cost ?? 0);
      return `${tokens} tokens · $${cost.toFixed(4)}`;
    }
    default:
      return text || row.event;
  }
}

/** 压缩空白 + 截断（列表行预览用）。 */
export function previewText(text: string, limit = 160): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/** 字节数 → 可读体量（KB/MB，一位小数）。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** 秒级时间戳 → 本地时间（无效值给空串，列表不炸）。 */
export function formatTs(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  return new Date(seconds * 1000).toLocaleString();
}

/** 条目引用 → 目标层（"L1:chat/…" → "l1"；无法识别给空串）。 */
export function refLayer(ref: string): string {
  const index = ref.indexOf(":");
  return index > 0 ? ref.slice(0, index).toLowerCase() : "";
}
