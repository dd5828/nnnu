"use client";

/**
 * 记忆工作台（§7.10）：概览 / L1 分页 / L2·L3 文档 / 证据链图谱 / 整合触发 / 条目编辑。
 *
 * 整合是 202 后台跑：没有 WS 事件可等，靠 overview 的 `consolidating` 置位时
 * 轮询（1.5s）到收尾；条目改动后统一失效 ["memory"] 前缀，各视图自然刷新。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  ConsolidateResponse,
  MemoryDocsResponse,
  MemoryEntryView,
  MemoryGraphPayload,
  MemoryL1Page,
  MemoryOverview,
  SemanticGraphPayload,
} from "@/types/api";

export function useMemoryOverview() {
  return useQuery({
    queryKey: ["memory", "overview"],
    queryFn: () => apiFetch<MemoryOverview>("/api/v1/memory"),
    refetchInterval: (query) => (query.state.data?.consolidating ? 1500 : false),
  });
}

/** L1 行分页；file 传空串 = 取最新月文件（后端默认）。 */
export function useMemoryL1(surface: string, file: string, offset: number, limit = 200) {
  return useQuery({
    queryKey: ["memory", "l1", surface, file, offset, limit],
    queryFn: () => {
      const params = new URLSearchParams({
        surface,
        offset: String(offset),
        limit: String(limit),
      });
      if (file) {
        params.set("file", file);
      }
      return apiFetch<MemoryL1Page>(`/api/v1/memory/l1?${params.toString()}`);
    },
    enabled: Boolean(surface),
  });
}

/** 取一份 L2 文档（key=surface）或 L3 文档（key=doc 名）。 */
export function useMemoryDoc(layer: "l2" | "l3", key: string | null) {
  return useQuery({
    queryKey: ["memory", layer, key],
    queryFn: () => {
      const params = new URLSearchParams();
      params.set(layer === "l2" ? "surface" : "doc", key ?? "");
      return apiFetch<MemoryDocsResponse>(`/api/v1/memory/${layer}?${params.toString()}`);
    },
    enabled: Boolean(key),
  });
}

/** 证据链：entry 为空 = 全景（L3→L2）；指定 entry + depth=2 = 展开到 L1。 */
export function useMemoryGraph(entry: string | null, depth: 1 | 2, enabled = true) {
  return useQuery({
    queryKey: ["memory", "graph", entry, depth],
    queryFn: () => {
      const params = new URLSearchParams({ depth: String(depth) });
      if (entry) {
        params.set("entry", entry);
      }
      return apiFetch<MemoryGraphPayload>(`/api/v1/memory/graph?${params.toString()}`);
    },
    enabled,
  });
}

/** 语义知识图谱（§7.10 补做）：独立派生工件，无展开语义（entry/depth 传了会被 422）。 */
export function useSemanticGraph(enabled: boolean) {
  return useQuery({
    queryKey: ["memory", "graph", "semantic"],
    queryFn: () => apiFetch<SemanticGraphPayload>("/api/v1/memory/graph?mode=semantic"),
    enabled,
  });
}

/** 手动「立即整合」：后台跑，202 回来。busy 时后端 409（前端按钮先置灰）。 */
export function useConsolidateMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<ConsolidateResponse>("/api/v1/memory/consolidate", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
}

/** 改条目正文（人工编辑 → 保护集）或交还自动管理（managed=true）。 */
export function usePatchMemoryEntry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { layer: "l2" | "l3"; id: string; text?: string; managed?: boolean }) =>
      apiFetch<{ layer: string; key: string; entry: MemoryEntryView }>(
        `/api/v1/memory/${input.layer}/${input.id}`,
        {
          method: "PATCH",
          body: JSON.stringify(input.managed ? { managed: true } : { text: input.text ?? "" }),
        }
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
}

/** 删条目（后端记 tombstone，防整合把已删条目又写回来）。 */
export function useDeleteMemoryEntry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { layer: "l2" | "l3"; id: string }) =>
      apiFetch<{ deleted: string }>(`/api/v1/memory/${input.layer}/${input.id}`, {
        method: "DELETE",
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
}
