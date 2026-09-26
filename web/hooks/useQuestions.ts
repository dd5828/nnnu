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
import type { AttemptResponse, Question, QuestionFilter, QuestionListResponse } from "@/types/api";

export interface QuestionFilters {
  filter: QuestionFilter;
  knowledgePoint?: string;
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
      filters.search ?? "",
      filters.nodeId ?? "",
    ],
    queryFn: () => apiFetch<QuestionListResponse>(`/api/v1/questions?${queryString(filters)}`),
    enabled: filters.enabled ?? true,
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

export function useUpdateQuestion() {
  const invalidate = useInvalidateQuestions();
  return useMutation({
    mutationFn: ({ id, ...body }: QuestionInput & { id: string }) =>
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
