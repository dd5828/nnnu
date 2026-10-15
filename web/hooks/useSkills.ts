"use client";

/**
 * 技能读写（§7.16）：TanStack Query，增删改/安装后失效列表与详情。
 *
 * 市场目录单独一条缓存：`refresh` 令牌进 queryKey——点「刷新目录」就是换一条查询
 * （带 ?refresh=1 强刷远程），不做手动过期。创建/保存返回的详情直接进缓存，
 * 列表随后失效重取（来源、文件数、安全门报告都会变）。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { SkillDetail, SkillHubDetail, SkillHubResponse, SkillSummary } from "@/types/api";

export function useSkillList() {
  return useQuery({
    queryKey: ["skills", "list"],
    queryFn: () => apiFetch<{ skills: SkillSummary[] }>("/api/v1/skills"),
  });
}

export function useSkill(name: string | null) {
  return useQuery({
    queryKey: ["skills", "detail", name],
    queryFn: async () => (await apiFetch<{ skill: SkillDetail }>(`/api/v1/skills/${name}`)).skill,
    enabled: Boolean(name),
  });
}

export interface CreateSkillInput {
  name: string;
  content: string;
  files?: Record<string, string>;
}

export interface UpdateSkillInput {
  name: string;
  enabled?: boolean;
  content?: string;
  files?: Record<string, string>;
}

function useInvalidateSkills() {
  const queryClient = useQueryClient();
  return useCallback(
    (name?: string) => {
      void queryClient.invalidateQueries({ queryKey: ["skills", "list"] });
      if (name) {
        void queryClient.invalidateQueries({ queryKey: ["skills", "detail", name] });
      }
    },
    [queryClient]
  );
}

export function useCreateSkill() {
  const invalidate = useInvalidateSkills();
  return useMutation({
    mutationFn: async (body: CreateSkillInput) =>
      (
        await apiFetch<{ skill: SkillDetail }>("/api/v1/skills", {
          method: "POST",
          body: JSON.stringify(body),
        })
      ).skill,
    onSuccess: (skill) => invalidate(skill.name),
  });
}

export function useUpdateSkill() {
  const invalidate = useInvalidateSkills();
  return useMutation({
    mutationFn: async ({ name, ...body }: UpdateSkillInput) =>
      (
        await apiFetch<{ skill: SkillDetail }>(`/api/v1/skills/${name}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        })
      ).skill,
    onSuccess: (skill) => invalidate(skill.name),
  });
}

export function useDeleteSkill() {
  const invalidate = useInvalidateSkills();
  return useMutation({
    mutationFn: (name: string) => apiFetch(`/api/v1/skills/${name}`, { method: "DELETE" }),
    onSuccess: (_data, name) => invalidate(name),
  });
}

/** 市场目录（内置 + 远程）；refresh 是点击「刷新」时自增的令牌。 */
export function useSkillHub(refresh = 0) {
  return useQuery({
    queryKey: ["skills", "hub", refresh],
    queryFn: () => apiFetch<SkillHubResponse>(`/api/v1/skills/hub${refresh ? "?refresh=1" : ""}`),
  });
}

/** 市场条目详情：含安装前安全门预扫描报告。 */
export function useSkillHubDetail(slug: string | null) {
  return useQuery({
    queryKey: ["skills", "hub", "detail", slug],
    queryFn: () => apiFetch<SkillHubDetail>(`/api/v1/skills/hub/${slug}`),
    enabled: Boolean(slug),
  });
}

export function useInstallSkill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ slug, confirm }: { slug: string; confirm: boolean }) =>
      (
        await apiFetch<{ skill: SkillDetail }>(`/api/v1/skills/hub/${slug}/install`, {
          method: "POST",
          body: JSON.stringify({ confirm }),
        })
      ).skill,
    onSuccess: (skill) => {
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
      void queryClient.invalidateQueries({ queryKey: ["skills", "detail", skill.name] });
    },
  });
}
