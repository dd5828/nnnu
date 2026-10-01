"use client";

/** 知识中心列表（§7.9）：全库检索 + 库卡片 + 建库向导（建完直接进详情页）。 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Database, Loader2, Plus } from "lucide-react";
import CreateWizard from "@/components/knowledge/CreateWizard";
import KnowledgeList from "@/components/knowledge/KnowledgeList";
import SearchBench from "@/components/knowledge/SearchBench";
import { useKbList } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";
import { kbDeepLink } from "@/lib/citations";
import { errorText } from "@/lib/errors";
import type { KbHit } from "@/types/api";

export default function KnowledgePage() {
  const { t } = useI18n();
  const router = useRouter();
  const { data, isLoading, error } = useKbList();
  const [wizardOpen, setWizardOpen] = useState(false);
  const kbs = data?.kbs ?? [];
  // 一个建过索引的库都没有时，检索区不出现（没东西可搜）
  const hasSearchable = kbs.some((kb) => kb.status === "ready" && kb.active_version > 0);

  // 命中自报库 id：跳到它所属库的详情页，阅读器直接开在那份文档（与引用点击同一条深链接）
  const openHit = (hit: KbHit) => router.push(kbDeepLink(hit.kb_id, hit.doc_id, hit.page));

  return (
    <main className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <div>
            <h1 className="text-xl font-semibold">{t("knowledge.title")}</h1>
            <p className="mt-0.5 text-xs text-muted">{t("knowledge.desc")}</p>
          </div>
          {!wizardOpen && (
            <button
              type="button"
              data-testid="kb-new"
              onClick={() => setWizardOpen(true)}
              className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("knowledge.newKb")}
            </button>
          )}
        </div>

        {wizardOpen && <CreateWizard onClose={() => setWizardOpen(false)} />}

        {hasSearchable && (
          <section
            data-testid="search-all"
            className="rounded-xl border border-border bg-surface p-5"
          >
            <h2 className="text-base font-semibold">{t("knowledge.searchAllTitle")}</h2>
            <p className="mt-0.5 mb-3 text-xs text-muted">{t("knowledge.searchAllDesc")}</p>
            <SearchBench onOpen={openHit} />
          </section>
        )}

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
        ) : kbs.length === 0 && !wizardOpen ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
            <Database className="h-6 w-6 text-muted" />
            <p className="text-sm text-muted">{t("knowledge.empty")}</p>
            <button
              type="button"
              onClick={() => setWizardOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
            >
              <Plus className="h-3.5 w-3.5" />
              {t("knowledge.newKb")}
            </button>
          </div>
        ) : (
          <KnowledgeList kbs={kbs} />
        )}
      </div>
    </main>
  );
}
