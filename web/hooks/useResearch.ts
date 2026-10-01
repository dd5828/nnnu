"use client";

/**
 * 调研历史（§7.6 的「我的历次调研」）：TanStack Query 三个端点（列表/详情/删除）。
 *
 * 没有轮询：调研在飞时有 WS 事件在推，这个页面只负责翻已经落库的账。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { ResearchRunDetail, ResearchRunListResponse, ResearchRunStatus } from "@/types/api";

/** 在飞的两态（判据跟后端 409 那条一致）：删了能力层没法收尾，按钮先置灰。 */
export function isResearchRunActive(status: ResearchRunStatus): boolean {
  return status === "confirming" || status === "researching";
}

export function useResearchRuns() {
  return useQuery({
    queryKey: ["research", "runs"],
    queryFn: () => apiFetch<ResearchRunListResponse>("/api/v1/research/runs"),
  });
}

export function useResearchRun(id: string | null) {
  return useQuery({
    queryKey: ["research", "run", id],
    queryFn: () => apiFetch<ResearchRunDetail>(`/api/v1/research/runs/${id}`),
    enabled: Boolean(id),
  });
}

/** 删一行调研历史（在飞的两态后端会 409，前端把按钮先置灰）。 */
export function useDeleteResearchRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ deleted: string }>(`/api/v1/research/runs/${id}`, { method: "DELETE" }),
    // 只失效列表：详情页删完就离开，顺手失效它会让它在卸载前把 404 闪出来
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["research", "runs"] }),
  });
}
