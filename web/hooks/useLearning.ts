"use client";

/**
 * 学习路径读写（§7.5 / §9.1）：TanStack Query。
 *
 * 路径由聊天里的 `mastery` 工具读写（REST 不建路径、不推进），这里负责看板读、
 * 树编辑与起学习会话。所有写操作都失效 `["learning"]`——一次编辑会同时动树、
 * 汇总与薄弱点，粒度再细也躲不开这三处一起变。
 *
 * **没有推进接口**：门就是游标，下一步由服务端每回合现算（`next_target`）。
 *
 * `useStartSession` 不改任何服务端数据（回合在后台跑），但照样失效一次：
 * 会话绑定与下一目标可能已经变了，回来时看板得是新的。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  LearningNode,
  LearningPath,
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

/** 起/续学习会话：服务端绑定路径并开一个 mastery_path 回合，正文在 WS 上流。 */
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
