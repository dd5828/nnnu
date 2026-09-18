"use client";

/** 后端心跳：30s 轮询（"首页经代理访问后端成功"的可见验收物）。 */

import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { HealthResponse } from "@/types/api";

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => apiFetch<HealthResponse>("/api/v1/health"),
    refetchInterval: 30_000,
    retry: 1,
  });
}
