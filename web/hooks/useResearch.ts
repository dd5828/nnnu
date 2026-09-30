"use client";

/**
 * 调研历史（§7.6 的「我的历次调研」，只读）：TanStack Query 两个端点。
 *
 * 没有轮询：调研在飞时有 WS 事件在推，这个页面只负责翻已经落库的账。
 */

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { ResearchRunDetail, ResearchRunListResponse } from "@/types/api";

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
