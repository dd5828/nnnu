"use client";

/**
 * CLI 应用读写（§7.18）：TanStack Query，改动后整体失效 `["cli-apps"]`。
 *
 * 安装/卸载都是**服务端真在跑命令**的动作，可能十几秒到几分钟（npm/pip 下载）。
 * 所以这里不给乐观更新：装没装上以程序真的找不找得到为准，猜出来的状态只会骗人。
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { CliApp, CliAppInstallResponse, CliAppsResponse } from "@/types/api";

/** 安装/卸载超时（ms）：pip/npm 真在下载，后端每条命令给 300 秒（建 venv + 装是两条）。
 *  默认那 30 秒会把还在装的请求掐掉——界面上显示「失败」而服务端其实刚装好。 */
export const INSTALL_TIMEOUT_MS = 12 * 60_000;

export function useCliApps(query = "") {
  const q = query.trim();
  return useQuery({
    queryKey: ["cli-apps", "list", q],
    queryFn: () =>
      apiFetch<CliAppsResponse>(`/api/v1/cli-apps${q ? `?q=${encodeURIComponent(q)}` : ""}`),
    // 目录是静态数据，状态靠 invalidate 刷新；输入搜索词时不闪回上一次结果
    placeholderData: (previous) => previous,
  });
}

export function useCliApp(id: string | null) {
  return useQuery({
    queryKey: ["cli-apps", "detail", id],
    queryFn: () => apiFetch<{ app: CliApp }>(`/api/v1/cli-apps/${id}`).then((data) => data.app),
    enabled: Boolean(id),
  });
}

function useInvalidateCliApps() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    // 装一件应用会改「有没有装」的判定口径与挂载门，整体重取最省心（条目本来就小）
    void queryClient.invalidateQueries({ queryKey: ["cli-apps"] });
  }, [queryClient]);
}

/** 安装（或对自带的应用做一次「找得到吗」的检测）：confirm 由调用方在确认后置 true。 */
export function useInstallCliApp() {
  const invalidate = useInvalidateCliApps();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<CliAppInstallResponse>("/api/v1/cli-apps", {
        method: "POST",
        body: JSON.stringify({ id, confirm: true }),
        // 真实下载可能跑几分钟，别用默认那 30 秒把还在装的请求掐掉
        signal: AbortSignal.timeout(INSTALL_TIMEOUT_MS),
      }),
    onSuccess: () => invalidate(),
  });
}

export function useUninstallCliApp() {
  const invalidate = useInvalidateCliApps();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/api/v1/cli-apps/${id}`, {
        method: "DELETE",
        signal: AbortSignal.timeout(INSTALL_TIMEOUT_MS),
      }),
    onSuccess: () => invalidate(),
  });
}
