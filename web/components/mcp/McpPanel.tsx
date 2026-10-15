"use client";

/**
 * MCP 标签页（§7.17）：服务器清单 + 新建/编辑弹窗。
 *
 * 状态刷新的口径：连上/掉线/重试都是后端后台自己在跑，页面没有推送通道，所以这里
 * 只在两种时刻主动重取——mutate 之后（hook 里已失效）和用户点「刷新状态」。不做轮询：
 * 一个只在改配置和排障时看一眼的页面，定时请求换不来什么，只换来后台日志。
 */

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, Server } from "lucide-react";
import McpAddDialog from "@/components/mcp/McpAddDialog";
import McpServerCard from "@/components/mcp/McpServerCard";
import { useI18n } from "@/hooks/useI18n";
import { useMcpServers } from "@/hooks/useMcp";
import { errorText } from "@/lib/errors";
import type { McpServer } from "@/types/api";

type DialogState = { mode: "closed" } | { mode: "new" } | { mode: "edit"; server: McpServer };

export default function McpPanel() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useMcpServers();
  const [dialog, setDialog] = useState<DialogState>({ mode: "closed" });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["mcp", "servers"] });
  };

  if (dialog.mode === "new") {
    return <McpAddDialog server={null} onClose={() => setDialog({ mode: "closed" })} />;
  }
  if (dialog.mode === "edit") {
    return <McpAddDialog server={dialog.server} onClose={() => setDialog({ mode: "closed" })} />;
  }

  const servers = data?.servers ?? [];

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <p className="text-xs text-muted">{t("mcp.desc")}</p>
        <button
          type="button"
          data-testid="mcp-add"
          onClick={() => setDialog({ mode: "new" })}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("mcp.add")}
        </button>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
      ) : servers.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
          <Server className="h-6 w-6 text-muted" />
          <p className="text-sm text-muted">{t("mcp.empty")}</p>
          <p className="max-w-sm text-xs text-muted">{t("mcp.emptyHint")}</p>
        </div>
      ) : (
        <ul className="space-y-2">
          {servers.map((server) => (
            <McpServerCard
              key={server.id}
              server={server}
              onEdit={(target) => setDialog({ mode: "edit", server: target })}
              onRefresh={refresh}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
