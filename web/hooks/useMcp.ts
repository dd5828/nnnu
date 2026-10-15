"use client";

/**
 * MCP 服务器读写（§7.17）：TanStack Query，改动后整体失效 `["mcp"]`。
 *
 * 这里的写操作**只有一种成功**：配置保存成功（带密钥时还要求先探通）。连不上不是
 * 异常路径，是 400 + 一句原因 + 探测详情——所以换密钥的失败也要把原因原样显示，
 * 「保存了但连不上」是用户最白忙的一种成功，后端已经拦住了，前端别再自己吞掉。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { McpCatalogEntry, McpProbeResult, McpServer, McpServerInput } from "@/types/api";

export function useMcpServers() {
  return useQuery({
    queryKey: ["mcp", "servers"],
    queryFn: () => apiFetch<{ servers: McpServer[] }>("/api/v1/mcp"),
  });
}

/** 内置目录（一键预填）：静态清单，缓存久一点无所谓。 */
export function useMcpCatalog() {
  return useQuery({
    queryKey: ["mcp", "catalog"],
    queryFn: () => apiFetch<{ entries: McpCatalogEntry[] }>("/api/v1/mcp/catalog"),
    staleTime: 10 * 60_000,
  });
}

function useInvalidateMcp() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    // 列表与目录都挂在 ["mcp"] 下：多一台服务器就多一批工具，
    // 状态/清单/掩码都可能变，整体重取最省心（数据量本来就小）
    void queryClient.invalidateQueries({ queryKey: ["mcp"] });
  }, [queryClient]);
}

export function useCreateMcpServer() {
  const invalidate = useInvalidateMcp();
  return useMutation({
    mutationFn: async (body: McpServerInput) =>
      (
        await apiFetch<{ server: McpServer }>("/api/v1/mcp", {
          method: "POST",
          body: JSON.stringify(body),
        })
      ).server,
    onSuccess: () => invalidate(),
  });
}

export function useUpdateMcpServer() {
  const invalidate = useInvalidateMcp();
  return useMutation({
    mutationFn: async ({ id, ...body }: McpServerInput & { id: string }) =>
      (
        await apiFetch<{ server: McpServer }>(`/api/v1/mcp/${id}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        })
      ).server,
    onSuccess: () => invalidate(),
  });
}

export function useDeleteMcpServer() {
  const invalidate = useInvalidateMcp();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/mcp/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(),
  });
}

/** 测试连接：一次性探测，不动活连接（结果里带工具清单，先看看能拿到什么）。 */
export function useTestMcpServer() {
  return useMutation({
    mutationFn: async ({ id, secrets }: { id: string; secrets?: Record<string, string> }) =>
      apiFetch<McpProbeResult>(`/api/v1/mcp/${id}/test`, {
        method: "POST",
        body: JSON.stringify({ secrets: secrets ?? {} }),
      }),
  });
}

/** 开一次 OAuth 同意流程：返回授权 URL（后端也会顺手弹浏览器）。 */
export function useAuthorizeMcpServer() {
  const invalidate = useInvalidateMcp();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ auth_url: string }>(`/api/v1/mcp/${id}/auth`, { method: "POST" }),
    onSuccess: () => invalidate(),
  });
}
