"use client";

/**
 * 知识库详情（§7.9）：文档表 + 添加文件 + 重建 + 检索测试台 + 阅读器。
 *
 * 支持深链接 `?doc=<doc_id>&page=N`——聊天里的引用就点到这里，进来直接开阅读器
 * 并跳到那一页（验收里的「引用点击定位」）。
 */

import { useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { ArrowLeft, FilePlus2, Loader2, RefreshCw, Square } from "lucide-react";
import { useCancelBuild, useKb, useReindex, useUploadQueue } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";
import type { KbDoc, KbHit } from "@/types/api";
import DocTable from "./DocTable";
import KnowledgeReader from "./KnowledgeReader";
import SearchBench from "./SearchBench";
import { formatSize, KbStatusChip, ProgressBar } from "./status";

interface OpenDoc {
  doc: KbDoc;
  page: number | null;
}

export default function KnowledgeDetail() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const kbId = params?.id ?? null;
  const { data: kb, isLoading, error } = useKb(kbId);
  const reindex = useReindex(kbId ?? "");
  const cancelBuild = useCancelBuild(kbId ?? "");
  const { uploads, enqueue } = useUploadQueue(kbId);
  const fileInput = useRef<HTMLInputElement>(null);
  const [manualReader, setManualReader] = useState<OpenDoc | null>(null);
  const [deepLinkClosed, setDeepLinkClosed] = useState(false);

  // 引用跳过来：?doc=…&page=N。这份「想开哪个文档」直接由 URL 推出来，
  // 用户点开别的文档（manualReader）优先，关掉深链接那个之后就别再自动弹。
  const deepLinkReader = useMemo<OpenDoc | null>(() => {
    const docId = search.get("doc");
    if (!kb || !docId) {
      return null;
    }
    const doc = kb.docs.find((item) => item.doc_id === docId && item.status !== "deleted");
    if (!doc) {
      return null; // 文档没了，深链接当没这回事
    }
    const pageParam = Number(search.get("page") ?? "");
    return { doc, page: Number.isInteger(pageParam) && pageParam > 0 ? pageParam : null };
  }, [kb, search]);

  const reader = manualReader ?? (deepLinkClosed ? null : deepLinkReader);
  const openDoc = (doc: KbDoc, page: number | null = null) => setManualReader({ doc, page });
  const closeReader = () => {
    setManualReader(null);
    setDeepLinkClosed(true);
  };
  const openHit = (hit: KbHit) => {
    const doc = kb?.docs.find((item) => item.doc_id === hit.doc_id);
    if (doc) {
      openDoc(doc, hit.page ?? null);
    }
  };

  if (isLoading) {
    return (
      <main className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </main>
    );
  }

  if (error || !kb) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-3">
        <p className="text-sm text-muted">{t("knowledge.notFound")}</p>
        <Link href="/knowledge" className="text-sm text-primary underline">
          {t("knowledge.backToList")}
        </Link>
      </main>
    );
  }

  const docs = kb.docs.filter((doc) => doc.status !== "deleted");
  const activeVersion = kb.versions.find((version) => version.version === kb.active_version);

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-center gap-2">
          <Link
            href="/knowledge"
            title={t("knowledge.backToList")}
            className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <h1 className="truncate text-xl font-semibold">{kb.name}</h1>
          <KbStatusChip status={kb.status} />
          <span className="text-xs text-muted">
            {kb.active_version > 0
              ? t("knowledge.versionShort", { n: String(kb.active_version) })
              : t("knowledge.noVersion")}
            {activeVersion
              ? ` · ${t("knowledge.chunkCount", { n: String(activeVersion.chunk_count) })}`
              : ""}
          </span>
        </div>

        {kb.error && <p className="text-sm text-danger">{kb.error}</p>}
        {kb.build && (
          <div className="rounded-xl border border-border bg-surface p-4">
            <div className="flex items-center gap-2 text-sm">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
              <span>{t("knowledge.rebuildingHint", { n: String(kb.build.version) })}</span>
              {kb.build.note && <span className="text-xs text-muted">{kb.build.note}</span>}
              <button
                type="button"
                onClick={() => cancelBuild.mutate()}
                className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs transition-colors hover:bg-accent"
              >
                <Square className="h-3 w-3" />
                {t("knowledge.cancelBuild")}
              </button>
            </div>
            <div className="mt-2">
              <ProgressBar value={kb.build.progress} />
            </div>
          </div>
        )}

        <section className="rounded-xl border border-border bg-surface p-5">
          <div className="mb-3 flex items-center gap-2">
            <h2 className="text-base font-semibold">{t("knowledge.docsTitle")}</h2>
            <span className="text-xs text-muted">
              {t("knowledge.docCount", { n: String(docs.length) })}
            </span>
            <div className="ml-auto flex items-center gap-2">
              <input
                ref={fileInput}
                data-testid="kb-add-files"
                type="file"
                multiple
                onChange={(e) => {
                  const picked = Array.from(e.target.files ?? []);
                  e.target.value = ""; // 允许重复选同一个文件
                  void enqueue(picked);
                }}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent"
              >
                <FilePlus2 className="h-3.5 w-3.5" />
                {t("knowledge.addFiles")}
              </button>
              <button
                type="button"
                data-testid="kb-reindex"
                onClick={() => reindex.mutate()}
                disabled={Boolean(kb.build)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent disabled:opacity-50"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                {kb.build ? t("knowledge.reindexing") : t("knowledge.reindex")}
              </button>
            </div>
          </div>

          {uploads.length > 0 && (
            <div className="mb-3 space-y-1.5 rounded-lg bg-accent/40 p-3">
              {uploads.map((item, index) => (
                <div key={`${item.name}-${index}`} className="text-xs">
                  <div className="flex items-center gap-2">
                    <span className="truncate">{item.name}</span>
                    <span className="ml-auto shrink-0 text-muted">
                      {item.error
                        ? item.error
                        : item.done
                          ? formatSize(item.size)
                          : t("knowledge.uploading", {
                              percent: String(Math.round(item.ratio * 100)),
                            })}
                    </span>
                  </div>
                  {!item.done && (
                    <div className="mt-1">
                      <ProgressBar value={item.ratio} />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          <DocTable kbId={kb.id} docs={docs} onOpen={(doc) => openDoc(doc)} />
        </section>

        <section className="rounded-xl border border-border bg-surface p-5">
          <h2 className="mb-3 text-base font-semibold">{t("knowledge.searchTitle")}</h2>
          <SearchBench kbId={kb.id} onOpen={openHit} />
        </section>
      </div>

      {reader && (
        <KnowledgeReader kbId={kb.id} doc={reader.doc} page={reader.page} onClose={closeReader} />
      )}
    </main>
  );
}
