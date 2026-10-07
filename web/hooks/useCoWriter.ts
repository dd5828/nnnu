"use client";

/**
 * Co-Writer 读写（§7.13）：TanStack Query。
 *
 * 两个和别处不一样的约定：
 * - 自动保存（PATCH）成功后只失效列表，**不**失效详情缓存——详情是编辑器的
 *   初始装载源，失效重拉会打断正在打的字；正文一致性靠「发起改写前 flush、
 *   accept 前后 cancel/本地写回」这套编排保证（见工作台页）；
 * - 发起改写单独把超时放宽到 120s：整段扩写 + 检索是模型调用，30s 默认档不够。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  CoWriterAcceptResponse,
  CoWriterAction,
  CoWriterDoc,
  CoWriterDocDetail,
  CoWriterDocSummary,
  CoWriterEditResult,
  CoWriterRejectResponse,
} from "@/types/api";

/** 改写请求超时（ms）：对话级模型调用，跟着 api.ts 的 30s 档不够用。 */
export const EDIT_TIMEOUT_MS = 120_000;

export function useCoWriterList() {
  return useQuery({
    queryKey: ["co-writer"],
    queryFn: () => apiFetch<{ docs: CoWriterDocSummary[] }>("/api/v1/co-writer"),
  });
}

/** 详情（含整篇正文）：工作台装载时读一次，之后正文由页面本地状态接管。 */
export function useCoWriterDoc(id: string | null) {
  return useQuery({
    queryKey: ["co-writer-doc", id],
    queryFn: () => apiFetch<CoWriterDocDetail>(`/api/v1/co-writer/${id}`),
    enabled: Boolean(id),
  });
}

export function useCreateCoWriterDoc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { title?: string; content?: string }) =>
      apiFetch<CoWriterDoc>("/api/v1/co-writer", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["co-writer"] }),
  });
}

export function useDeleteCoWriterDoc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/co-writer/${id}`, { method: "DELETE" }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["co-writer"] }),
  });
}

export interface SaveCoWriterDocInput {
  id: string;
  title?: string;
  content?: string;
}

/** 自动保存 / 重命名（PATCH）：只失效列表，卡片标题与预览跟着更新。 */
export function useSaveCoWriterDoc() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: SaveCoWriterDocInput) =>
      apiFetch<CoWriterDoc>(`/api/v1/co-writer/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["co-writer"] }),
  });
}

export interface EditCoWriterInput {
  docId: string;
  /** 选区的**码点**下标（textarea 的 UTF-16 下标先过 codePointIndex）。 */
  start: number;
  end: number;
  original: string;
  action: CoWriterAction;
  instruction?: string;
  language: string;
  kbIds?: string[];
  useWeb?: boolean;
}

/** 发起改写：结果挂在调用方（弹层要拿住 edit_id/ops）。此请求不改文档。 */
export function useCoWriterEdit() {
  return useMutation({
    mutationFn: ({ docId, kbIds, useWeb, ...body }: EditCoWriterInput) =>
      apiFetch<CoWriterEditResult>(`/api/v1/co-writer/${docId}/edit`, {
        method: "POST",
        body: JSON.stringify({ ...body, kb_ids: kbIds ?? [], use_web: useWeb ?? false }),
        signal: AbortSignal.timeout(EDIT_TIMEOUT_MS),
      }),
  });
}

/** 确认写回：正文本地写回由调用方做（lib/co-writer.ts 的 applyEdit），
 *  这里只负责服务端那半 + 缓存失效。同一条编辑只能消费一次。 */
export function useAcceptCoWriterEdit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ docId, editId }: { docId: string; editId: string }) =>
      apiFetch<CoWriterAcceptResponse>(`/api/v1/co-writer/${docId}/edit/${editId}`, {
        method: "POST",
        body: JSON.stringify({ action: "accept" }),
      }),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["co-writer-doc", variables.docId] });
      void queryClient.invalidateQueries({ queryKey: ["co-writer"] });
    },
  });
}

/** 放弃：文档一分不动，只丢掉这条待确认编辑。 */
export function useRejectCoWriterEdit() {
  return useMutation({
    mutationFn: ({ docId, editId }: { docId: string; editId: string }) =>
      apiFetch<CoWriterRejectResponse>(`/api/v1/co-writer/${docId}/edit/${editId}`, {
        method: "POST",
        body: JSON.stringify({ action: "reject" }),
      }),
  });
}
