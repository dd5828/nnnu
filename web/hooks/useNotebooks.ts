"use client";

/**
 * 笔记本读写（§7.15 / §9.1）：TanStack Query，增删改后失效列表与详情。
 *
 * 没有轮询：这边不存在知识库那种后台构建进度，数据都是即写即读。
 * 移动/复制会同时失效**源本与目标本**（一条记录换本，两边条数都变）。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { Notebook, NotebookRecord, NotebookRecordType, RecordDetail } from "@/types/api";

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

/** 全局按 id 查记录（引用 chip 深链跟随用）：记录移动后也能找到它现在在哪本。
 *  404 是「引用失效」的正常分支，不重试。 */
export function useRecord(recordId: string | null) {
  return useQuery({
    queryKey: ["notebook-record", recordId],
    queryFn: () => apiFetch<RecordDetail>(`/api/v1/notebooks/records/${recordId}`),
    enabled: Boolean(recordId),
    retry: false,
  });
}

export function useUpdateNotebook() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; name?: string; description?: string }) =>
      apiFetch<Notebook>(`/api/v1/notebooks/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: (notebook) => invalidate(notebook.id),
  });
}

export interface UpdateRecordInput {
  notebookId: string;
  recordId: string;
  title?: string;
  content_md?: string;
  /** 与当前归属不同 = 移动；只改归属列，记录 id 不变（引用不失效的根）。 */
  targetNotebookId?: string;
}

/** 编辑 + 移动合一个 PATCH（§9.1 P9 偏离关闭）：后端一条请求先移后改。 */
export function useUpdateRecord() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: ({ notebookId, recordId, targetNotebookId, ...body }: UpdateRecordInput) =>
      apiFetch<NotebookRecord>(`/api/v1/notebooks/${notebookId}/records/${recordId}`, {
        method: "PATCH",
        body: JSON.stringify({ ...body, target_notebook_id: targetNotebookId }),
      }),
    onSuccess: (record, variables) => {
      invalidate(variables.notebookId); // 源本少一条
      invalidate(record.notebook_id); // 目标本多一条
    },
  });
}

export interface CopyRecordInput {
  notebookId: string;
  recordId: string;
  /** 不给 = 复制回原本（同本留个副本）。 */
  targetNotebookId?: string;
}

/** 复制成新记录（新 nbr- id）：原记录不动。 */
export function useCopyRecord() {
  const invalidate = useInvalidateNotebooks();
  return useMutation({
    mutationFn: ({ notebookId, recordId, targetNotebookId }: CopyRecordInput) =>
      apiFetch<NotebookRecord>(`/api/v1/notebooks/${notebookId}/records/${recordId}/copy`, {
        method: "POST",
        body: JSON.stringify({ target_notebook_id: targetNotebookId }),
      }),
    onSuccess: (record, variables) => {
      invalidate(variables.notebookId);
      invalidate(record.notebook_id);
    },
  });
}
