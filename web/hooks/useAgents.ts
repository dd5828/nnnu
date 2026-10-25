"use client";

/**
 * 子代理读写（§7.12）：TanStack Query，改动后整体失效 `["agents"]`。
 *
 * 三件事跟别处不一样，都是有原因的：
 *
 * - **探测不缓存、也不乐观**。`probe` 是后端真起一个进程跑 `<程序> --version`，
 *   结果只代表「刚才那一秒」，所以它是个 mutation 而不是 query：用户点一次就是
 *   一次真检查，界面别自己算「上次探到了，现在应该也行」——本机就躺着一个反例
 *   （npm 的 claude 三件套都在 PATH 上，跑起来只有一行报错）。
 * - **扫描/导入给长超时**。扫描要 rglob 整棵历史目录，导入要逐条解析并落盘，
 *   默认 30 秒会把「还在扫」当成「扫炸了」。超时按后端上限留足余量。
 * - **导入之后再取列表**，不做本地拼接：服务层按 id + 摘要判「新增/更新/没变」，
 *   前端自己猜哪条进去了只会跟后端口径对不上。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  AgentConnectionResponse,
  AgentConnectionsResponse,
  AgentImportDetailResponse,
  AgentImportListResponse,
  AgentImportResult,
  AgentImportScanResponse,
  AgentProbeResponse,
} from "@/types/api";

/** 扫描/导入的超时（ms）：服务端要遍历历史目录、逐条解析落盘，慢慢来是正常的。 */
export const AGENT_SCAN_TIMEOUT_MS = 3 * 60_000;

/** 建连接的入参（`acknowledged` 是界面上那个知情勾选，服务端不给建，见 router 模块头）。 */
export interface AgentConnectionInput {
  kind: string;
  display_name?: string;
  cwd?: string;
  model_override?: string;
  permission_mode?: string;
  command?: string[];
  acknowledged: boolean;
}

export function useAgentConnections() {
  return useQuery({
    queryKey: ["agents", "connections"],
    queryFn: () => apiFetch<AgentConnectionsResponse>("/api/v1/agents"),
  });
}

/** 已导入的历史会话（标题与前段正文可搜）。 */
export function useAgentImports(query = "", options?: { enabled?: boolean }) {
  const q = query.trim();
  return useQuery({
    queryKey: ["agents", "imports", "list", q],
    queryFn: () =>
      apiFetch<AgentImportListResponse>(
        `/api/v1/agents/imports${q ? `?q=${encodeURIComponent(q)}` : ""}`
      ),
    // 边打字边搜：输入变化时先留着上一次的结果，别闪成空白
    placeholderData: (previous) => previous,
    enabled: options?.enabled ?? true,
  });
}

/** 一条导入会话的正文（分页）。`id` 为空时不请求（深链没带 id 的情况）。 */
export function useAgentImport(id: string | null, offset = 0, limit = 50) {
  return useQuery({
    queryKey: ["agents", "imports", "detail", id, offset, limit],
    queryFn: () =>
      apiFetch<AgentImportDetailResponse>(
        `/api/v1/agents/imports/${encodeURIComponent(id ?? "")}?offset=${offset}&limit=${limit}`
      ),
    enabled: Boolean(id),
  });
}

function useInvalidateAgents() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    // 连接与导入都挂在 ["agents"] 下：多一条连接会改工具挂载门（一条都没有就不挂
    // consult_subagent），导入会改列表与搜索结果，整体重取最省心（数据量本来就小）
    void queryClient.invalidateQueries({ queryKey: ["agents"] });
  }, [queryClient]);
}

export function useCreateAgentConnection() {
  const invalidate = useInvalidateAgents();
  return useMutation({
    mutationFn: async (body: AgentConnectionInput) =>
      (
        await apiFetch<AgentConnectionResponse>("/api/v1/agents", {
          method: "POST",
          body: JSON.stringify(body),
        })
      ).connection,
    onSuccess: () => invalidate(),
  });
}

export function useDeleteAgentConnection() {
  const invalidate = useInvalidateAgents();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/agents/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(),
  });
}

/** 探测：真跑一次 `--version`。返回值给调用方就地显示，不进缓存（见模块头）。 */
export function useProbeAgentConnection() {
  return useMutation({
    mutationFn: async (id: string) =>
      (
        await apiFetch<AgentProbeResponse>(`/api/v1/agents/${id}/probe`, {
          method: "POST",
          // 后端给这次探测 8 秒；多留点余量兜住调度抖动
          signal: AbortSignal.timeout(20_000),
        })
      ).probe,
  });
}

/** 扫候选：只读不写，所以是 mutation 而不是 query——按天数是用户点出来的动作。 */
export function useScanAgentImports() {
  return useMutation({
    mutationFn: ({ root = "", days = 0 }: { root?: string; days?: number }) =>
      apiFetch<AgentImportScanResponse>(
        `/api/v1/agents/imports/candidates?days=${days}${
          root ? `&root=${encodeURIComponent(root)}` : ""
        }`,
        { signal: AbortSignal.timeout(AGENT_SCAN_TIMEOUT_MS) }
      ),
  });
}

/** 导入勾中的（`ids` 为空 = 导入扫到的全部；幂等，没变的不重写）。 */
export function useImportAgentSessions() {
  const invalidate = useInvalidateAgents();
  return useMutation({
    mutationFn: (body: { root?: string; days?: number; ids?: string[] }) =>
      apiFetch<AgentImportResult>("/api/v1/agents/imports", {
        method: "POST",
        body: JSON.stringify({ root: "", days: 0, ids: [], ...body }),
        signal: AbortSignal.timeout(AGENT_SCAN_TIMEOUT_MS),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteAgentImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/api/v1/agents/imports/${encodeURIComponent(id)}`, { method: "DELETE" }),
    // 只让**列表**失效，别整个 `["agents"]` 一起失效：删掉的正是当前摊开的那条时，那个正文
    // query 还挂着观察者，一起失效就会拿着刚删掉的 id 再取一次——必然 404，而且浏览器把它
    // 记成一条 console error（「console 零报错」的验收当场红）。行没了列表一刷就收起。
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents", "imports", "list"] }),
  });
}
