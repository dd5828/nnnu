"use client";

/** 文档表（§7.9）：每份文档的状态、进度、页码/切块数，失败的可单独删（验收 D）。 */

import { FileText, Trash2 } from "lucide-react";
import { useDeleteDoc } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";
import type { KbDoc } from "@/types/api";
import { DocStatusChip, formatSize, ProgressBar } from "./status";

const IN_FLIGHT = new Set(["parsing", "chunking", "embedding"]);

interface DocTableProps {
  kbId: string;
  docs: KbDoc[];
  onOpen: (doc: KbDoc) => void;
}

export default function DocTable({ kbId, docs, onOpen }: DocTableProps) {
  const { t } = useI18n();
  const remove = useDeleteDoc(kbId);

  const handleDelete = (doc: KbDoc) => {
    if (window.confirm(t("knowledge.deleteDocConfirm", { name: doc.filename }))) {
      remove.mutate(doc.doc_id);
    }
  };

  if (docs.length === 0) {
    return <p className="py-6 text-center text-sm text-muted">{t("knowledge.noDocs")}</p>;
  }

  return (
    <ul className="divide-y divide-border/60">
      {docs.map((doc) => (
        <li key={doc.doc_id} data-testid="doc-row" className="flex items-center gap-3 py-2.5">
          <FileText className="h-4 w-4 shrink-0 text-muted" />
          <button
            type="button"
            onClick={() => onOpen(doc)}
            className="min-w-0 flex-1 text-left"
            title={t("knowledge.reader")}
          >
            <span className="block truncate text-sm hover:text-primary">{doc.filename}</span>
            <span className="mt-0.5 flex items-center gap-2 text-xs text-muted">
              <DocStatusChip status={doc.status} />
              <span>{formatSize(doc.size)}</span>
              {doc.page_count > 0 && (
                <span>{t("knowledge.pageCount", { n: String(doc.page_count) })}</span>
              )}
              {doc.chunk_count > 0 && (
                <span>{t("knowledge.chunkCount", { n: String(doc.chunk_count) })}</span>
              )}
              {IN_FLIGHT.has(doc.status) && doc.note && <span>{doc.note}</span>}
            </span>
            {IN_FLIGHT.has(doc.status) && (
              <span className="mt-1 block max-w-xs">
                <ProgressBar value={doc.progress} />
              </span>
            )}
            {doc.status === "error" && doc.error && (
              <span className="mt-0.5 block truncate text-xs text-danger">{doc.error}</span>
            )}
          </button>
          <button
            type="button"
            data-testid="doc-delete"
            onClick={() => handleDelete(doc)}
            title={t("knowledge.deleteDoc")}
            className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </li>
      ))}
    </ul>
  );
}
