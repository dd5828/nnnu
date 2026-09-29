"use client";

/**
 * 学习路径读写（§7.5 / §9.1）：TanStack Query。
 *
 * 路径由聊天里那八个 `mastery_*` 工具读写（REST 不建路径、不推进），这里负责看板读、
 * 树编辑、看板操作与起学习会话。所有写操作都失效 `["learning"]`——一次编辑会同时动树、
 * 汇总与薄弱点，粒度再细也躲不开这三处一起变。
 *
 * **没有推进接口**：门就是游标，下一步由服务端每回合现算（`next_target`）。
 *
 * `useStartSession` 只把这条路径的会话备好（绑定 + 标题），**不跑回合**——
 * 第一句由用户在聊天里自己打。它照样失效一次缓存：会话绑定变了，回来时看板得是新的。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  DueReviewListResponse,
  LearningNode,
  LearningPath,
  LearningPathActionResponse,
  LearningPathDetail,
  LearningPathListResponse,
  LearningSessionResponse,
} from "@/types/api";

function useInvalidateLearning() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["learning"] });
  }, [queryClient]);
}

export function useLearningPaths() {
  return useQuery({
    queryKey: ["learning", "paths"],
    queryFn: () => apiFetch<LearningPathListResponse>("/api/v1/learning/paths"),
  });
}

export function useLearningPath(id: string | null) {
  return useQuery({
    queryKey: ["learning", "path", id ?? ""],
    queryFn: () => apiFetch<LearningPathDetail>(`/api/v1/learning/paths/${id}`),
    enabled: Boolean(id),
  });
}

/** 打开这条路径的聊天：服务端备好会话并绑上路径，**零 LLM**——不替用户开口。 */
export function useStartSession() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: ({ pathId, language }: { pathId: string; language: string }) =>
      apiFetch<LearningSessionResponse>(`/api/v1/learning/paths/${pathId}/session`, {
        method: "POST",
        body: JSON.stringify({ language }),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useUpdatePath() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: string;
      title?: string;
      topic?: string;
      summary?: string;
    }) =>
      apiFetch<LearningPath>(`/api/v1/learning/paths/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useDeletePath() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/learning/paths/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(),
  });
}

export interface NodeInput {
  title: string;
  node_type: string;
  description?: string;
  parent_id?: string | null;
}

export function useAddNode() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: ({ pathId, ...body }: NodeInput & { pathId: string }) =>
      apiFetch<LearningNode>(`/api/v1/learning/paths/${pathId}/nodes`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useUpdateNode() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: string;
      title?: string;
      node_type?: string;
      description?: string;
    }) =>
      apiFetch<LearningNode>(`/api/v1/learning/nodes/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteNode() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/learning/nodes/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(),
  });
}

/** 同父兄弟上下移：返回整棵新树（前序），直接顶掉详情缓存。 */
export function useMoveNode() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: ({ id, direction }: { id: string; direction: "up" | "down" }) =>
      apiFetch<LearningPathDetail>(`/api/v1/learning/nodes/${id}/move`, {
        method: "POST",
        body: JSON.stringify({ direction }),
      }),
    onSuccess: () => invalidate(),
  });
}

/** 跳过当前未决的那道题：卡作废，掌握度与作答历史都不动（不是判错）。 */
export function useSkipQuestion() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: (pathId: string) =>
      apiFetch<LearningPathActionResponse>(`/api/v1/learning/paths/${pathId}/skip-question`, {
        method: "POST",
      }),
    onSuccess: () => invalidate(),
  });
}

/** 重做整条路径：掌握度/评定/复习/作答历史全清，节点树与题目留着。 */
export function useRedoPath() {
  const invalidate = useInvalidateLearning();
  return useMutation({
    mutationFn: (pathId: string) =>
      apiFetch<LearningPathActionResponse>(`/api/v1/learning/paths/${pathId}/redo`, {
        method: "POST",
      }),
    onSuccess: () => invalidate(),
  });
}

/** 跨路径的到期复习聚合（看板「该复习了」）。 */
export function useDueReviews() {
  return useQuery({
    queryKey: ["learning", "reviews"],
    queryFn: () => apiFetch<DueReviewListResponse>("/api/v1/learning/reviews"),
  });
}
