"use client";

/** 外部库（§7.9 P14）：Obsidian 挂载与笔记读写、IMA 浏览检索、MarginNote 接收端。 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  ImaItem,
  ImaPage,
  LibrariesOverview,
  MarginNoteDevice,
  MarginNoteObject,
  ObsidianNote,
  ObsidianNoteSummary,
  ObsidianSearchHit,
  ObsidianVault,
} from "@/types/api";

export function useLibrariesOverview() {
  return useQuery({
    queryKey: ["libraries"],
    queryFn: () => apiFetch<LibrariesOverview>("/api/v1/libraries"),
  });
}

function useInvalidateLibraries() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["libraries"] });
  };
}

// ================= Obsidian =================

export function useAddVault() {
  const invalidate = useInvalidateLibraries();
  return useMutation({
    mutationFn: (input: { path: string; name: string }) =>
      apiFetch<{ vault: ObsidianVault }>("/api/v1/libraries/obsidian/vaults", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: invalidate,
  });
}

export function useRemoveVault() {
  const invalidate = useInvalidateLibraries();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vaultId: string) =>
      apiFetch(`/api/v1/libraries/obsidian/vaults/${vaultId}`, { method: "DELETE" }),
    onSuccess: () => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ["obsidian-notes"] });
      void queryClient.invalidateQueries({ queryKey: ["obsidian-note"] });
    },
  });
}

export function useObsidianNotes(vaultId: string | null, folder = "") {
  return useQuery({
    queryKey: ["obsidian-notes", vaultId, folder],
    queryFn: () =>
      apiFetch<{ vault: ObsidianVault; notes: ObsidianNoteSummary[]; folder: string }>(
        `/api/v1/libraries/obsidian/vaults/${vaultId}/notes?folder=${encodeURIComponent(folder)}`
      ),
    enabled: Boolean(vaultId),
  });
}

export function useObsidianNote(vaultId: string | null, path: string | null) {
  return useQuery({
    queryKey: ["obsidian-note", vaultId, path],
    queryFn: () =>
      apiFetch<ObsidianNote>(
        `/api/v1/libraries/obsidian/vaults/${vaultId}/note?path=${encodeURIComponent(path ?? "")}`
      ),
    enabled: Boolean(vaultId && path),
  });
}

export function useObsidianSearch(vaultId: string | null, query: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["obsidian-search", vaultId, trimmed],
    queryFn: () =>
      apiFetch<{ hits: ObsidianSearchHit[]; query: string }>(
        `/api/v1/libraries/obsidian/vaults/${vaultId}/search?q=${encodeURIComponent(trimmed)}`
      ),
    enabled: Boolean(vaultId && trimmed),
  });
}

export function useWriteObsidianNote(vaultId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { path: string; content: string; mode: "overwrite" | "append" }) =>
      apiFetch<{ path: string }>(`/api/v1/libraries/obsidian/vaults/${vaultId}/note`, {
        method: "PUT",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["obsidian-notes", vaultId] });
      // 写回响应只有路径/大小，没有正文；让这个 vault 的笔记查询重新取全文，
      // 别拿半份数据覆盖缓存（键里的 path 可能没带 .md，不能按单条精确匹配）
      void queryClient.invalidateQueries({ queryKey: ["obsidian-note", vaultId] });
      void queryClient.invalidateQueries({ queryKey: ["libraries"] });
    },
  });
}

// ================= 腾讯 IMA =================

export function useImaTest() {
  return useMutation({
    mutationFn: () =>
      apiFetch<{ ok: boolean; name: string; error: string }>("/api/v1/libraries/ima/test", {
        method: "POST",
      }),
  });
}

export function useImaBrowse(enabled: boolean, folderId = "") {
  return useQuery({
    queryKey: ["ima-browse", folderId],
    queryFn: () =>
      apiFetch<ImaPage>(
        `/api/v1/libraries/ima/browse?folder_id=${encodeURIComponent(folderId)}&limit=50`
      ),
    enabled,
  });
}

export function useImaSearch(query: string) {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["ima-search", trimmed],
    queryFn: () =>
      apiFetch<ImaPage>(`/api/v1/libraries/ima/search?q=${encodeURIComponent(trimmed)}&limit=20`),
    enabled: Boolean(trimmed),
  });
}

export function useImaItem(mediaId: string | null) {
  return useQuery({
    queryKey: ["ima-item", mediaId],
    queryFn: () => apiFetch<ImaItem>(`/api/v1/libraries/ima/items/${mediaId}`),
    enabled: Boolean(mediaId),
  });
}

// ================= MarginNote =================

export function useMnDevices() {
  return useQuery({
    queryKey: ["mn-devices"],
    queryFn: () =>
      apiFetch<{ devices: MarginNoteDevice[] }>("/api/v1/libraries/marginnote/devices"),
  });
}

export function useMnPair() {
  const invalidate = useInvalidateLibraries();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { device_name: string; device_kind: string }) =>
      apiFetch<{ device: MarginNoteDevice; token: string }>("/api/v1/libraries/marginnote/pair", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ["mn-devices"] });
    },
  });
}

export function useMnRevoke() {
  const invalidate = useInvalidateLibraries();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (deviceId: string) =>
      apiFetch<{ device: MarginNoteDevice }>(`/api/v1/libraries/marginnote/devices/${deviceId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ["mn-devices"] });
    },
  });
}

export function useMnObjects(query: string, objectType = "") {
  const trimmed = query.trim();
  return useQuery({
    queryKey: ["mn-objects", trimmed, objectType],
    queryFn: () => {
      const params = new URLSearchParams();
      if (trimmed) {
        params.set("q", trimmed);
      }
      if (objectType) {
        params.set("object_type", objectType);
      }
      params.set("limit", "50");
      return apiFetch<{
        count: number;
        objects?: MarginNoteObject[];
        results?: MarginNoteObject[];
      }>(`/api/v1/libraries/marginnote/objects?${params.toString()}`);
    },
  });
}
