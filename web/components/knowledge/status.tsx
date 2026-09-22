"use client";

/** 知识库状态零件（§7.9）：状态徽标 + 进度条，列表/文档表/向导共用一套色。 */

import { useI18n } from "@/hooks/useI18n";
import type { KbDocStatus, KbStatus } from "@/types/api";

const KB_STATUS_KEY: Record<KbStatus, string> = {
  creating: "knowledge.statusCreating",
  indexing: "knowledge.statusIndexing",
  ready: "knowledge.statusReady",
  error: "knowledge.statusError",
};

const KB_STATUS_CLASS: Record<KbStatus, string> = {
  creating: "border-border text-muted",
  indexing: "border-primary/40 text-primary",
  ready: "border-success/40 text-success",
  error: "border-danger/40 text-danger",
};

export function KbStatusChip({ status }: { status: KbStatus }) {
  const { t } = useI18n();
  return (
    <span
      data-testid="kb-status"
      data-status={status}
      className={`rounded-full border px-2 py-0.5 text-[11px] ${
        KB_STATUS_CLASS[status] ?? KB_STATUS_CLASS.error
      }`}
    >
      {t(KB_STATUS_KEY[status] ?? "knowledge.statusError")}
    </span>
  );
}

const DOC_STATUS_KEY: Record<KbDocStatus, string> = {
  parsing: "knowledge.docStatusParsing",
  chunking: "knowledge.docStatusChunking",
  embedding: "knowledge.docStatusEmbedding",
  done: "knowledge.docStatusDone",
  error: "knowledge.docStatusError",
  deleted: "knowledge.docStatusDeleted",
};

const DOC_STATUS_CLASS: Record<KbDocStatus, string> = {
  parsing: "text-primary",
  chunking: "text-primary",
  embedding: "text-primary",
  done: "text-success",
  error: "text-danger",
  deleted: "text-muted",
};

export function DocStatusChip({ status }: { status: KbDocStatus }) {
  const { t } = useI18n();
  return (
    <span
      data-testid="doc-status"
      data-status={status}
      className={`text-xs ${DOC_STATUS_CLASS[status] ?? "text-muted"}`}
    >
      {t(DOC_STATUS_KEY[status] ?? "knowledge.docStatusError")}
    </span>
  );
}

/** 阶段进度条：value 是 0~1，超界夹紧（后端偶尔会给到 1.0001 之类的浮点尾巴）。 */
export function ProgressBar({ value }: { value: number }) {
  const percent = Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      data-testid="progress"
      data-percent={percent}
      className="h-1.5 w-full overflow-hidden rounded-full bg-accent"
    >
      <div
        className="h-full rounded-full bg-primary transition-all"
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

/** 文件大小：KB/MB 一刀切，够看一眼就行。 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
