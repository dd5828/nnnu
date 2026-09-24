"use client";

/** 设置区读写：TanStack Query 缓存 + PUT 后失效重取。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { LlmOptionsResponse, SettingsAreaResponse } from "@/types/api";

export function useSettingsArea(area: string) {
  return useQuery({
    queryKey: ["settings", area],
    queryFn: () => apiFetch<SettingsAreaResponse>(`/api/v1/settings/${area}`),
  });
}

/** 聊天输入区模型选择器的选项（§6.10）：注册表快照 + 当前默认。
 *  staleTime 0 是因为设置页的草稿-应用走裸 apiFetch，不进 react-query 缓存，
 *  只能靠每次进聊天重取来反映"刚在设置页改了密钥/默认"。 */
export function useLlmOptions() {
  return useQuery({
    queryKey: ["llm-options"],
    queryFn: () => apiFetch<LlmOptionsResponse>("/api/v1/settings/llm-options"),
    staleTime: 0,
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
