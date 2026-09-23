"use client";

/** 知识库选择器（§7.9，对齐上游 DeepTutor）：会话级粘性 + 多选，默认不检索。
 *
 * 选中 = 本会话往后每个回合都能用 rag 查这些库；一个都不选就不挂 rag 工具，
 * 聊天完全不碰知识库。选择随每条消息全量下发给后端落库，所以切会话/刷新后仍在。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Database } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { useKbList } from "@/hooks/useKnowledge";

export default function KnowledgeSelector() {
  const { t } = useI18n();
  const kbIds = useChatStore((s) => s.kbIds);
  const setKbIds = useChatStore((s) => s.setKbIds);
  const { data } = useKbList();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // 只列真的能搜的库（建库中/索引出错的不给选）；修剪选择时另按全量列表算——
  // 正在重建索引的库不该因为"暂时不可选"被悄悄丢掉
  const kbs = useMemo(
    () => (data?.kbs ?? []).filter((kb) => kb.status === "ready" && kb.active_version > 0),
    [data]
  );
  const knownIds = (data?.kbs ?? []).map((kb) => kb.id).join(",");

  useEffect(() => {
    if (data === undefined) {
      return; // 列表还没回来：此时修剪会把刚水合的选择误清空
    }
    const known = new Set(knownIds ? knownIds.split(",") : []);
    const pruned = kbIds.filter((id) => known.has(id));
    if (pruned.length !== kbIds.length) {
      setKbIds(pruned);
    }
    // kbIds 修剪后回灌，进依赖会自激；判断依据只有列表本身
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, knownIds]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const count = kbIds.length;
  const label =
    count === 0
      ? t("chat.knowledge")
      : count === 1
        ? kbs.find((kb) => kb.id === kbIds[0])?.name ?? t("chat.knowledge")
        : t("chat.knowledgeCount", { n: String(count) });

  const toggle = (id: string) =>
    setKbIds(kbIds.includes(id) ? kbIds.filter((item) => item !== id) : [...kbIds, id]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="kb-selector"
        onClick={() => setOpen((prev) => !prev)}
        aria-label={t("chat.selectKnowledgeBases")}
        aria-expanded={open}
        title={t("chat.knowledgeHint")}
        className={`flex max-w-[10rem] items-center gap-1.5 rounded-lg p-2 text-xs transition-colors ${
          count > 0
            ? "text-primary hover:bg-primary/10"
            : "text-muted hover:bg-accent hover:text-foreground"
        }`}
      >
        <Database className="h-4 w-4 shrink-0" />
        <span className="truncate">{label}</span>
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(280px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface shadow-lg">
          {kbs.length === 0 ? (
            <div className="px-3 py-4 text-center text-xs text-muted">
              {t("chat.noKnowledgeBases")}
            </div>
          ) : (
            <div className="max-h-[280px] overflow-y-auto py-1">
              {kbs.map((kb) => {
                const active = kbIds.includes(kb.id);
                return (
                  <button
                    key={kb.id}
                    type="button"
                    data-testid="kb-option"
                    onClick={() => toggle(kb.id)}
                    className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-xs transition-colors ${
                      active ? "bg-primary/5" : "hover:bg-accent"
                    }`}
                  >
                    <Database
                      className={`h-3.5 w-3.5 shrink-0 ${active ? "text-primary" : "text-muted"}`}
                    />
                    <span className="min-w-0 flex-1 truncate font-medium">{kb.name}</span>
                    {active && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
