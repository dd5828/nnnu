"use client";

/** 知识库列表（§7.9）：一张卡一个库——状态、文档/切块数、当前版本，点进去看详情。 */

import Link from "next/link";
import { FileText, Layers, Trash2 } from "lucide-react";
import { useDeleteKb } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";
import type { KbManifest } from "@/types/api";
import { KbStatusChip, ProgressBar } from "./status";

export default function KnowledgeList({ kbs }: { kbs: KbManifest[] }) {
  const { t } = useI18n();
  const remove = useDeleteKb();

  const handleDelete = (kb: KbManifest) => {
    if (window.confirm(t("knowledge.deleteKbConfirm", { name: kb.name }))) {
      remove.mutate(kb.id);
    }
  };

  return (
    <ul className="space-y-2">
      {kbs.map((kb) => {
        const live = kb.docs.filter((doc) => doc.status !== "deleted");
        const chunks = kb.versions.find((v) => v.version === kb.active_version)?.chunk_count ?? 0;
        // 建库/重建中：进度条显示构建进度；否则显示「已索引文档占比」当装饰
        const progress = kb.build
          ? kb.build.progress
          : live.length > 0
            ? live.filter((doc) => doc.status === "done").length / live.length
            : 0;
        return (
          <li
            key={kb.id}
            data-testid="kb-card"
            className="rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40"
          >
            <div className="flex items-start gap-3">
              <Link href={`/knowledge/${kb.id}`} className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium">{kb.name}</span>
                  <KbStatusChip status={kb.status} />
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted">
                  <span className="inline-flex items-center gap-1">
                    <FileText className="h-3.5 w-3.5" />
                    {t("knowledge.docCount", { n: String(live.length) })}
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <Layers className="h-3.5 w-3.5" />
                    {t("knowledge.chunkCount", { n: String(chunks) })}
                  </span>
                  <span>
                    {kb.active_version > 0
                      ? t("knowledge.versionShort", { n: String(kb.active_version) })
                      : t("knowledge.noVersion")}
                  </span>
                </div>
                {kb.status === "error" && kb.error && (
                  <p className="mt-1 text-xs text-danger">{kb.error}</p>
                )}
                {kb.build && (
                  <p className="mt-1 text-xs text-muted">
                    {t("knowledge.rebuildingHint", { n: String(kb.build.version) })}
                    {kb.build.note ? ` · ${kb.build.note}` : ""}
                  </p>
                )}
              </Link>
              <button
                type="button"
                onClick={() => handleDelete(kb)}
                title={t("knowledge.deleteKb")}
                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
            {(kb.status === "creating" || kb.status === "indexing" || kb.build) && (
              <div className="mt-3">
                <ProgressBar value={progress} />
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
