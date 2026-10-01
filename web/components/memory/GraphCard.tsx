"use client";

/**
 * 记忆图谱卡片（§7.10 补做）：证据链与语义知识图谱两种模式共用一个外壳。
 *
 * - 证据链（默认）：L3→L2→L1，L3/L2 节点可点击展开（root 状态在这里）；
 * - 语义知识图谱：derived 工件（consolidator extract 产出），无展开语义；
 * - 切模式清空 root：不把证据链的展开态带进语义图（后端 semantic 拒绝 entry）。
 */

import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import MemoryGraph from "@/components/memory/MemoryGraph";
import SemanticGraph from "@/components/memory/SemanticGraph";
import { useI18n } from "@/hooks/useI18n";
import { useMemoryGraph, useSemanticGraph } from "@/hooks/useMemory";
import { errorText } from "@/lib/errors";

type Mode = "evidence" | "semantic";

export default function GraphCard() {
  const { t } = useI18n();
  const [mode, setMode] = useState<Mode>("evidence");
  const [rootId, setRootId] = useState<string | null>(null);

  const graph = useMemoryGraph(rootId, rootId ? 2 : 1, mode === "evidence");
  const semanticGraph = useSemanticGraph(mode === "semantic");

  const rawError = mode === "evidence" ? graph.error : semanticGraph.error;
  const error = rawError ? errorText(rawError, t("common.requestFailed")) : null;

  const switchMode = (next: Mode) => {
    setMode(next);
    setRootId(null);
  };

  return (
    <section data-testid="memory-graph" className="rounded-xl border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t("memory.graphTitle")}</h2>
        <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
          {(["evidence", "semantic"] as const).map((value) => (
            <button
              key={value}
              type="button"
              data-testid={`memory-graph-mode-${value}`}
              aria-pressed={mode === value}
              onClick={() => switchMode(value)}
              className={`rounded px-2.5 py-1 text-xs ${
                mode === value ? "bg-primary text-primary-foreground" : "text-muted hover:bg-accent"
              }`}
            >
              {t(value === "evidence" ? "memory.graphModeEvidence" : "memory.graphModeSemantic")}
            </button>
          ))}
        </div>
        <span className="flex-1" />
        {mode === "evidence" && rootId && (
          <button
            type="button"
            data-testid="graph-reset"
            onClick={() => setRootId(null)}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-accent"
          >
            <ArrowLeft className="size-3.5" />
            {t("memory.graphReset")}
          </button>
        )}
      </div>
      <p className="mt-0.5 text-xs text-muted">
        {mode === "evidence"
          ? rootId
            ? t("memory.graphExpandedHint")
            : t("memory.graphHint")
          : t("memory.semanticHint")}
      </p>

      {mode === "evidence" ? (
        <MemoryGraph
          payload={graph.data}
          isLoading={graph.isLoading}
          error={error}
          rootId={rootId}
          onExpand={setRootId}
        />
      ) : (
        <SemanticGraph
          payload={semanticGraph.data}
          isLoading={semanticGraph.isLoading}
          error={error}
        />
      )}
    </section>
  );
}
