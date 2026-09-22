"use client";

/**
 * 检索测试台（§7.9）：在库里直接试 query，看混合与纯向量的差别。
 *
 * 命中的 score 只在这一次检索内有意义（向量是余弦 0~1，混合是 RRF 融合分
 * 1/(60+rank) 量级），所以显示出来只是为了对比排序，不跨模式比大小。
 */

import { useState } from "react";
import { Loader2, Search } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useI18n } from "@/hooks/useI18n";
import type { KbHit, KbSearchResponse } from "@/types/api";

const MODES = ["hybrid", "vector", "auto"] as const;
const TOP_K_CHOICES = [3, 5, 10];

export default function SearchBench({
  kbId,
  onOpen,
}: {
  kbId: string;
  onOpen: (hit: KbHit) => void;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<string>("hybrid");
  const [topK, setTopK] = useState(5);
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    const trimmed = query.trim();
    if (!trimmed || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await apiFetch<KbSearchResponse>(`/api/v1/kbs/${kbId}/search`, {
        method: "POST",
        body: JSON.stringify({ query: trimmed, mode, top_k: topK }),
      });
      setHits(result.hits);
    } catch (searchError) {
      setError(String(searchError));
      setHits(null);
    } finally {
      setBusy(false);
    }
  };

  const inputClass =
    "rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none transition-colors focus:border-primary/60";

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          data-testid="search-query"
          className={`${inputClass} min-w-40 flex-1`}
          placeholder={t("knowledge.searchPlaceholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              void run();
            }
          }}
        />
        <select
          data-testid="search-mode"
          className={inputClass}
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          {MODES.map((value) => (
            <option key={value} value={value}>
              {t(`knowledge.mode_${value}`)}
            </option>
          ))}
        </select>
        <select
          className={inputClass}
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value))}
        >
          {TOP_K_CHOICES.map((value) => (
            <option key={value} value={value}>
              {t("knowledge.topK", { n: String(value) })}
            </option>
          ))}
        </select>
        <button
          type="button"
          data-testid="search-run"
          onClick={() => void run()}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
        >
          {busy ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Search className="h-3.5 w-3.5" />
          )}
          {busy ? t("knowledge.searching") : t("knowledge.searchRun")}
        </button>
      </div>

      {error && <p className="mt-2 text-xs text-danger">{error}</p>}

      {hits && (
        <div className="mt-3" data-testid="search-results">
          <div className="mb-1 text-xs text-muted">
            {t("knowledge.hits", { n: String(hits.length) })}
          </div>
          {hits.length === 0 ? (
            <p className="text-sm text-muted">{t("knowledge.noHits")}</p>
          ) : (
            <ol className="space-y-2">
              {hits.map((hit, index) => (
                <li
                  key={`${hit.doc_id}-${hit.metadata.chunk_id ?? index}`}
                  data-testid="search-hit"
                  className="rounded-lg border border-border/70 bg-surface p-3"
                >
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span className="inline-block min-w-4 rounded bg-accent px-1 text-center font-medium text-primary">
                      {index + 1}
                    </span>
                    {hit.page !== null && hit.page !== undefined ? (
                      <span
                        data-testid="hit-page"
                        className="rounded bg-primary/10 px-1.5 py-0.5 font-medium text-primary"
                      >
                        {t("chat.page", { n: String(hit.page) })}
                      </span>
                    ) : (
                      <span>{t("knowledge.noPage")}</span>
                    )}
                    <span className="truncate">{String(hit.metadata.filename ?? hit.doc_id)}</span>
                    <span className="ml-auto shrink-0 font-mono">
                      {t("knowledge.score", { n: hit.score.toFixed(4) })}
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-4 text-sm">{hit.text}</p>
                  <button
                    type="button"
                    onClick={() => onOpen(hit)}
                    className="mt-1 text-xs text-primary underline"
                  >
                    {t("knowledge.reader")}
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
