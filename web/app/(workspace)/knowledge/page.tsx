"use client";

/** 知识中心列表（§7.9）：库卡片 + 建库向导（建完直接进详情页）。 */

import { useState } from "react";
import { Database, Loader2, Plus } from "lucide-react";
import CreateWizard from "@/components/knowledge/CreateWizard";
import KnowledgeList from "@/components/knowledge/KnowledgeList";
import { useKbList } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";

export default function KnowledgePage() {
  const { t } = useI18n();
  const { data, isLoading, error } = useKbList();
  const [wizardOpen, setWizardOpen] = useState(false);
  const kbs = data?.kbs ?? [];

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
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

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : error ? (
          <p className="text-sm text-danger">{String(error)}</p>
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
