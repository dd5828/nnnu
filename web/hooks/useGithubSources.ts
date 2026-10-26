"use client";

/** GitHub 源同步（§7.9 P14）：列表、绑定、解绑、检查漂移、手动同步。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { GithubSource, SourceSyncReport } from "@/types/api";

export interface AddGithubSourceInput {
  repo: string;
  branch: string;
  glob: string;
  auto_sync: boolean;
  interval_minutes: number;
}

export interface AddGithubSourceResult {
  source: GithubSource;
  sync: SourceSyncReport | null;
  sync_error: string;
}

export function useGithubSources(kbId: string) {
  return useQuery({
    queryKey: ["kb-sources", kbId],
    queryFn: () => apiFetch<{ sources: GithubSource[] }>(`/api/v1/kbs/${kbId}/sources`),
  });
}

function useInvalidateSources(kbId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["kb-sources", kbId] });
    void queryClient.invalidateQueries({ queryKey: ["kb", kbId] });
    void queryClient.invalidateQueries({ queryKey: ["kbs"] });
  };
}

export function useAddGithubSource(kbId: string) {
  const invalidate = useInvalidateSources(kbId);
  return useMutation({
    mutationFn: (input: AddGithubSourceInput) =>
      apiFetch<AddGithubSourceResult>(`/api/v1/kbs/${kbId}/sources`, {
        method: "POST",
        body: JSON.stringify({ ...input, sync_now: true }),
      }),
    onSuccess: invalidate,
  });
}

export function useDeleteGithubSource(kbId: string) {
  const invalidate = useInvalidateSources(kbId);
  return useMutation({
    mutationFn: (sourceId: string) =>
      apiFetch(`/api/v1/kbs/${kbId}/sources/${sourceId}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useCheckGithubSource(kbId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) =>
      apiFetch<SourceSyncReport>(`/api/v1/kbs/${kbId}/sources/${sourceId}/check`, {
        method: "POST",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["kb-sources", kbId] });
    },
  });
}

export function useSyncGithubSource(kbId: string) {
  const invalidate = useInvalidateSources(kbId);
  return useMutation({
    mutationFn: (sourceId: string) =>
      apiFetch<SourceSyncReport>(`/api/v1/kbs/${kbId}/sources/${sourceId}/sync`, {
        method: "POST",
      }),
    onSuccess: invalidate,
  });
}
