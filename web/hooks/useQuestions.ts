"use client";

/**
 * 题库读写（§7.4 / §9.1）：TanStack Query，增删改与作答后失效列表。
 *
 * 筛选参数进 queryKey：切筛选/知识点/关键词就是换一条查询，不用手动清缓存。
 * 作答（判分）返回的新题目状态也会就地写进缓存，省一次拉取（列表仍会失效重取，
 * 因为 counts 徽标跟着变）。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  AttemptResponse,
  BatchAdoptResponse,
  ClassifyResponse,
  ErrorCause,
  Question,
  QuestionFilter,
  QuestionListResponse,
  SimilarResponse,
  VariantsResponse,
} from "@/types/api";

export interface QuestionFilters {
  filter: QuestionFilter;
  knowledgePoint?: string;
  /** 按错因筛（枚举键；筛选条下拉用）。 */
  errorCause?: string;
  search?: string;
  /** 只看学习路径某个节点的题（学习看板的薄弱点深链 ?node= 走这里）。 */
  nodeId?: string;
  /** 拿不到节点时别发请求（节点详情面板还没选中节点）——默认发。 */
  enabled?: boolean;
}

function queryString(filters: QuestionFilters): string {
  const params = new URLSearchParams({ filter: filters.filter });
  if (filters.knowledgePoint) {
    params.set("knowledge_point", filters.knowledgePoint);
  }
  if (filters.errorCause) {
    params.set("error_cause", filters.errorCause);
  }
  if (filters.search) {
    params.set("search", filters.search);
  }
  if (filters.nodeId) {
    params.set("node_id", filters.nodeId);
  }
  return params.toString();
}

export function useQuestionList(filters: QuestionFilters) {
  return useQuery({
    queryKey: [
      "questions",
      filters.filter,
      filters.knowledgePoint ?? "",
      filters.errorCause ?? "",
      filters.search ?? "",
      filters.nodeId ?? "",
    ],
    queryFn: () => apiFetch<QuestionListResponse>(`/api/v1/questions?${queryString(filters)}`),
    enabled: filters.enabled ?? true,
  });
}

/** 单题查询（§7.1 引用深链 `?question=` 的置顶卡）：404 是「引用失效」的
 *  正常分支，不重试（对齐 useRecord 的口径）。 */
export function useQuestion(id: string | null) {
  return useQuery({
    queryKey: ["questions", "detail", id],
    queryFn: () => apiFetch<Question>(`/api/v1/questions/${id}`),
    enabled: Boolean(id),
    retry: false,
  });
}

/** 题目形态的提交体（新增与编辑共用；编辑走 PATCH，只发改动的字段也支持）。 */
export interface QuestionInput {
  stem: string;
  answer: string;
  options?: string[];
  type: string;
  explanation?: string | null;
  knowledge_point?: string;
  difficulty?: string;
  tags?: string[];
  error_causes?: ErrorCause[];
  note?: string;
}

function useInvalidateQuestions() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["questions"] });
  }, [queryClient]);
}

export function useCreateQuestion() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: (body: QuestionInput) =>
      apiFetch<Question>("/api/v1/questions", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => invalidate(),
  });
}

/** 局部更新：只发改动字段（笔记单独保存也走这里，body 可以只有 note）。 */
export function useUpdateQuestion() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<QuestionInput> & { id: string }) =>
      apiFetch<Question>(`/api/v1/questions/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteQuestion() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/questions/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(),
  });
}

/** 提交作答：后端判分 → 记作答 → 回题目新状态（掌握度、错误次数都变了）。 */
export function useSubmitAttempt() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: ({ id, answer, language }: { id: string; answer: string; language: string }) =>
      apiFetch<AttemptResponse>(`/api/v1/questions/${id}/attempt`, {
        method: "POST",
        body: JSON.stringify({ answer, language }),
      }),
    onSuccess: (data) => {
      void invalidate();
      return data;
    },
  });
}

/** 相似题（举一反三第一级）：零 LLM；面板打开才拉，同题短时间不重复拉。 */
export function useSimilarQuestions(questionId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["questions", "similar", questionId],
    queryFn: () => apiFetch<SimilarResponse>(`/api/v1/questions/${questionId}/similar?limit=5`),
    enabled,
    staleTime: 30_000,
  });
}

/** AI 分类：LLM 看题 + 笔记写回知识点/标签/错因（成功即刷列表——题目本体变了）。 */
export function useClassifyQuestion() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: ({ id, language }: { id: string; language: string }) =>
      apiFetch<ClassifyResponse>(`/api/v1/questions/${id}/classify`, {
        method: "POST",
        body: JSON.stringify({ language }),
      }),
    onSuccess: () => invalidate(),
  });
}

/** 生成变式题（预览态）：**不**失效列表——草稿不落库，勾选采纳时才变。 */
export function useGenerateVariants() {
  return useMutation({
    mutationFn: ({
      id,
      mode,
      count,
      language,
    }: {
      id: string;
      mode: string;
      count: number;
      language: string;
    }) =>
      apiFetch<VariantsResponse>(`/api/v1/questions/${id}/variants`, {
        method: "POST",
        body: JSON.stringify({ mode, count, language }),
      }),
  });
}

/** 采纳变式题（批量入库）：成功后列表、相似题、计数一起失效重取。 */
export function useAdoptVariants() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: ({ parentId, questions }: { parentId: string; questions: QuestionInput[] }) =>
      apiFetch<BatchAdoptResponse>("/api/v1/questions/batch", {
        method: "POST",
        body: JSON.stringify({ parent_id: parentId, questions }),
      }),
    onSuccess: () => invalidate(),
  });
}
