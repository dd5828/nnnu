"use client";

/**
 * 笔记本读写（§7.15 极简版 / §9.1）：TanStack Query，增删改后失效列表与详情。
 *
 * 没有轮询：这边不存在知识库那种后台构建进度，数据都是即写即读。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { Notebook, NotebookRecord, NotebookRecordType } from "@/types/api";

export function useNotebookList() {
  return useQuery({
    queryKey: ["notebooks"],
    queryFn: () => apiFetch<{ notebooks: Notebook[] }>("/api/v1/notebooks"),
  });
}

export function useNotebook(id: string | null) {
  return useQuery({
    queryKey: ["notebook", id],
    queryFn: () => apiFetch<Notebook>(`/api/v1/notebooks/${id}`),
    enabled: Boolean(id),
  });
}

/** 变更后同时刷新列表与详情（列表上显示记录条数）。 */
function useInvalidateNotebooks() {
  const queryClient = useQueryClient();
  return useCallback(
    (id?: string) => {
      void queryClient.invalidateQueries({ queryKey: ["notebooks"] });
      if (id) {
        void queryClient.invalidateQueries({ queryKey: ["notebook", id] });
      }
    },
    [queryClient]
  );
}

export function useCreateNotebook() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      apiFetch<Notebook>("/api/v1/notebooks", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (notebook) => invalidate(notebook.id),
  });
}

export function useDeleteNotebook() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/notebooks/${id}`, { method: "DELETE" }),
    onSuccess: (_data, id) => invalidate(id),
  });
}

/** 目标笔记本随调用一起给（详情页是当前那本，聊天里是弹层里现选的那本）。 */
export interface AddRecordInput {
  notebookId: string;
  type: NotebookRecordType;
  title?: string;
  content_md: string;
  source_ref?: string | null;
}

export function useAddRecord() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: ({ notebookId, ...body }: AddRecordInput) =>
      apiFetch<NotebookRecord>(`/api/v1/notebooks/${notebookId}/records`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (_record, variables) => invalidate(variables.notebookId),
  });
}

export function useDeleteRecord() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: ({ notebookId, recordId }: { notebookId: string; recordId: string }) =>
      apiFetch(`/api/v1/notebooks/${notebookId}/records/${recordId}`, { method: "DELETE" }),
    onSuccess: (_data, variables) => invalidate(variables.notebookId),
  });
}
