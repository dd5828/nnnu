"use client";

/**
 * 知识库读写（§7.9）：TanStack Query + 「处理中就轮询」的进度条。
 *
 * 后端把进度写在 manifest 里（doc.progress / doc.note / build.progress），前端
 * 唯一要做的就是慢速轮询——不是 WebSocket 推送：构建动辄几十秒，800ms 一次的
 * GET 足够顺滑，也省得为进度再开一条事件流。
 */

import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch } from "@/lib/api";
import type { KbDoc, KbManifest } from "@/types/api";

/** §7.9 要求索引进度实时更新：800ms 一次，肉眼看着像连续。 */
const POLL_MS = 800;

const IN_FLIGHT = new Set(["parsing", "chunking", "embedding"]);

/** 库或库里的文档还在动 → 继续轮询。 */
export function isKbBusy(manifest: KbManifest): boolean {
  if (manifest.status === "creating" || manifest.status === "indexing") {
    return true;
  }
  if (manifest.build) {
    return true;
  }
  return manifest.docs.some((doc) => IN_FLIGHT.has(doc.status));
}

export function useKbList() {
  return useQuery({
    queryKey: ["kbs"],
    queryFn: () => apiFetch<{ kbs: KbManifest[] }>("/api/v1/kbs"),
    refetchInterval: (query) => ((query.state.data?.kbs ?? []).some(isKbBusy) ? POLL_MS : false),
  });
}

export function useKb(id: string | null) {
  return useQuery({
    queryKey: ["kb", id],
    queryFn: () => apiFetch<KbManifest>(`/api/v1/kbs/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => (query.state.data && isKbBusy(query.state.data) ? POLL_MS : false),
  });
}

/** 变更后同时刷新列表与详情（两处都显示状态）。 */
function useInvalidateKb() {
  const queryClient = useQueryClient();
  return useCallback(
    (id?: string) => {
      void queryClient.invalidateQueries({ queryKey: ["kbs"] });
      if (id) {
        void queryClient.invalidateQueries({ queryKey: ["kb", id] });
      }
    },
    [queryClient]
  );
}

export function useCreateKb() {
  const invalidate = useInvalidateKb();
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<KbManifest>("/api/v1/kbs", {
        method: "POST",
        body: JSON.stringify({ name }),
      }),
    onSuccess: (manifest) => invalidate(manifest.id),
  });
}

export function useDeleteKb() {
  const invalidate = useInvalidateKb();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/kbs/${id}`, { method: "DELETE" }),
    onSuccess: (_data, id) => invalidate(id),
  });
}

export function useDeleteDoc(kbId: string) {
  const invalidate = useInvalidateKb();
  return useMutation({
    mutationFn: (docId: string) =>
      apiFetch(`/api/v1/kbs/${kbId}/docs/${docId}`, { method: "DELETE" }),
    onSuccess: () => invalidate(kbId),
  });
}

export function useReindex(kbId: string) {
  const invalidate = useInvalidateKb();
  return useMutation({
    mutationFn: () => apiFetch<KbManifest>(`/api/v1/kbs/${kbId}/reindex`, { method: "POST" }),
    onSuccess: () => invalidate(kbId),
  });
}

export function useCancelBuild(kbId: string) {
  const invalidate = useInvalidateKb();
  return useMutation({
    mutationFn: () => apiFetch(`/api/v1/kbs/${kbId}/build/cancel`, { method: "POST" }),
    onSuccess: () => invalidate(kbId),
  });
}

/**
 * 上传单个文档：走 `/api/upload/*` 专用转发口（proxy 的 rewrite 在 ~10MB 处
 * 会断请求体，KB 允许 50MB，见 web/proxy.ts），用 XHR 是为了拿到上传字节进度。
 *
 * 返回后端的 KbDoc（初始是 parsing，之后靠轮询刷新）。
 */
export function uploadKbDoc(
  kbId: string,
  file: File,
  onProgress?: (ratio: number) => void
): Promise<KbDoc> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload/kbs/${encodeURIComponent(kbId)}`);
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null; // 非 JSON（网关错误页之类），下面用状态码兜底
      }
      if (xhr.status >= 200 && xhr.status < 300 && body) {
        resolve(body as KbDoc);
        return;
      }
      // 与 apiFetch 同款错误信封解析，抛 ApiError 让调用方一视同仁
      const envelope = (
        body as { error?: { code?: string; message?: string; recoverable?: boolean } }
      )?.error;
      reject(
        new ApiError(
          xhr.status,
          envelope?.code ?? "upload_failed",
          envelope?.message ?? `上传失败（HTTP ${xhr.status}）`,
          envelope?.recoverable ?? false
        )
      );
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "上传失败：连接中断", true));
    xhr.send(form);
  });
}

export interface UploadItem {
  name: string;
  size: number;
  ratio: number; // 上传字节进度 0~1
  done: boolean;
  error: string | null;
}

/**
 * 上传队列：逐个传（串行好读，进度条不打架），传完刷新一下让详情页接上轮询。
 * `target` 是给建库向导用的——那边库刚创建出来，hook 手上的 kbId 还是空的。
 */
export function useUploadQueue(kbId: string | null) {
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [busy, setBusy] = useState(false);
  const invalidate = useInvalidateKb();

  const patch = (index: number, next: Partial<UploadItem>) =>
    setUploads((prev) => prev.map((item, i) => (i === index ? { ...item, ...next } : item)));

  const enqueue = useCallback(
    async (files: File[], target?: string) => {
      const kb = target ?? kbId;
      if (!kb || files.length === 0) {
        return;
      }
      setUploads(
        files.map((file) => ({
          name: file.name,
          size: file.size,
          ratio: 0,
          done: false,
          error: null,
        }))
      );
      setBusy(true);
      for (const [index, file] of files.entries()) {
        try {
          await uploadKbDoc(kb, file, (ratio) => patch(index, { ratio }));
          patch(index, { ratio: 1, done: true });
        } catch (error) {
          // 一份传失败不拦着后面的：库还在，详情页还能重传
          patch(index, { done: true, error: String(error) });
        }
      }
      setBusy(false);
      invalidate(kb);
    },
    [kbId, invalidate]
  );

  const clear = useCallback(() => setUploads([]), []);

  return { uploads, busy, enqueue, clear };
}
