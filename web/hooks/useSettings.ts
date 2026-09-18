"use client";

/** 设置区读写：TanStack Query 缓存 + PUT 后失效重取。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { SettingsAreaResponse } from "@/types/api";

export function useSettingsArea(area: string) {
  return useQuery({
    queryKey: ["settings", area],
    queryFn: () => apiFetch<SettingsAreaResponse>(`/api/v1/settings/${area}`),
  });
}

export function useUpdateSettings(area: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      apiFetch(`/api/v1/settings/${area}`, {
        method: "PUT",
        body: JSON.stringify({ values }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["settings", area] });
    },
  });
}
