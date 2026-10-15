"use client";

/**
 * 一台 MCP 服务器（§7.17）：状态、工具清单与逐个开关、测试连接、编辑/删除、OAuth 授权。
 *
 * 状态不是装饰：`error` 会把后端给的那句人话原样摆在卡片上（连不上的原因，用户只能从
 * 这儿看），`needs_auth` 才出现「去授权」——OAuth 服务器没授权之前重试多少次都没用，
 * 后端也不重试，这里更不能是空白。
 */

import { useState } from "react";
import { KeyRound, Loader2, Pencil, RefreshCw, Trash2 } from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import { useI18n } from "@/hooks/useI18n";
import {
  useAuthorizeMcpServer,
  useDeleteMcpServer,
  useTestMcpServer,
  useUpdateMcpServer,
} from "@/hooks/useMcp";
import { errorText } from "@/lib/errors";
import type { McpProbeResult, McpServer, McpStatus } from "@/types/api";

const STATUS_LABEL: Record<McpStatus, string> = {
  connected: "mcp.statusConnected",
  connecting: "mcp.statusConnecting",
  error: "mcp.statusError",
  needs_auth: "mcp.statusNeedsAuth",
  disabled: "mcp.statusDisabled",
};

const STATUS_CLASS: Record<McpStatus, string> = {
  connected: "text-success",
  connecting: "text-muted",
  error: "text-danger",
  needs_auth: "text-primary", // 没有 --warning 这个色，用主色：能操作的状态，不必像错误那样喊
  disabled: "text-muted",
};

interface Props {
  server: McpServer;
  onEdit: (server: McpServer) => void;
  onRefresh: () => void;
}

export default function McpServerCard({ server, onEdit, onRefresh }: Props) {
  const { t } = useI18n();
  const update = useUpdateMcpServer();
  const remove = useDeleteMcpServer();
  const test = useTestMcpServer();
  const authorize = useAuthorizeMcpServer();
  const [probe, setProbe] = useState<McpProbeResult | null>(null);

  const transportLabel =
    server.transport === "stdio"
      ? t("mcp.transportStdio")
      : server.transport === "sse"
        ? t("mcp.transportSse")
        : t("mcp.transportHttp");
  const oauth = server.credential?.kind === "oauth" || server.oauth !== null;

  const handleDelete = () => {
    void confirmDialog({
      message: t("mcp.deleteConfirm", { name: server.name }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        remove.mutate(server.id);
      }
    });
  };

  const toggleTool = (toolName: string, enabled: boolean) => {
    const next = enabled
      ? server.disabled_tools.filter((name) => name !== toolName)
      : [...server.disabled_tools, toolName];
    update.mutate({ id: server.id, disabled_tools: next });
  };

  const error = update.error ?? remove.error ?? authorize.error;

  return (
    <li
      data-testid="mcp-item"
      data-id={server.id}
      className={`rounded-xl border border-border bg-surface p-4 transition-colors hover:border-primary/40 ${
        server.enabled ? "" : "opacity-70"
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium">{server.name}</span>
            <span className="rounded bg-accent px-1.5 py-0.5 font-mono text-[10px] text-muted">
              {server.id}
            </span>
            <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
              {transportLabel}
            </span>
            <span
              data-testid={`mcp-status-${server.id}`}
              className={`text-[11px] ${STATUS_CLASS[server.status]}`}
            >
              {t(STATUS_LABEL[server.status])}
            </span>
            {server.status === "connecting" && (
              <Loader2 className="h-3 w-3 animate-spin text-muted" />
            )}
          </div>
          <p className="mt-0.5 truncate font-mono text-[11px] text-muted">
            {server.transport === "stdio"
              ? `${server.command} ${server.args.join(" ")}`
              : server.url}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] text-muted">
            <span>{t("mcp.toolsCount", { n: String(server.tools.length) })}</span>
            {oauth && (
              <span className={server.oauth?.authorized ? "text-success" : "text-primary"}>
                {server.oauth?.authorized ? t("mcp.authDone") : t("mcp.authPending")}
              </span>
            )}
          </div>
          {server.error && (
            <p data-testid={`mcp-error-${server.id}`} className="mt-1 text-xs text-danger">
              {server.error}
            </p>
          )}
        </div>

        <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-muted">
          <input
            type="checkbox"
            data-testid={`mcp-toggle-${server.id}`}
            checked={server.enabled}
            onChange={(event) => update.mutate({ id: server.id, enabled: event.target.checked })}
            className="h-4 w-4 accent-[var(--primary)]"
          />
          {t("mcp.enable")}
        </label>
        <button
          type="button"
          data-testid={`mcp-test-${server.id}`}
          disabled={test.isPending}
          onClick={() =>
            test.mutate(
              { id: server.id },
              {
                onSuccess: (result) => setProbe(result),
                onError: (err) =>
                  setProbe({
                    ok: false,
                    status: "error",
                    tools: [],
                    error: errorText(err, t("common.requestFailed")),
                  }),
              }
            )
          }
          className="shrink-0 rounded-lg border border-border px-2.5 py-1 text-xs transition-colors hover:bg-accent disabled:opacity-50"
        >
          {test.isPending ? t("mcp.testing") : t("mcp.test")}
        </button>
        <button
          type="button"
          data-testid={`mcp-refresh-${server.id}`}
          onClick={onRefresh}
          title={t("mcp.refresh")}
          aria-label={t("mcp.refresh")}
          className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <RefreshCw className="h-4 w-4" />
        </button>
        <button
          type="button"
          data-testid={`mcp-edit-${server.id}`}
          onClick={() => onEdit(server)}
          title={t("mcp.edit")}
          aria-label={t("mcp.edit")}
          className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <Pencil className="h-4 w-4" />
        </button>
        <button
          type="button"
          data-testid={`mcp-delete-${server.id}`}
          onClick={handleDelete}
          title={t("mcp.delete")}
          aria-label={t("mcp.delete")}
          className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>

      {error && (
        <p className="mt-2 text-xs text-danger">{errorText(error, t("common.requestFailed"))}</p>
      )}

      {server.status === "needs_auth" && !server.oauth?.authorized && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-background p-2">
          <p className="text-xs text-muted">{t("mcp.authHint")}</p>
          <button
            type="button"
            data-testid={`mcp-auth-${server.id}`}
            disabled={authorize.isPending}
            onClick={() => authorize.mutate(server.id)}
            className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3 py-1 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
          >
            <KeyRound className="h-3.5 w-3.5" />
            {authorize.isPending ? t("mcp.authing") : t("mcp.auth")}
          </button>
        </div>
      )}

      {probe && (
        <div
          data-testid={`mcp-probe-${server.id}`}
          className="mt-2 rounded-lg border border-border bg-background p-2 text-xs"
        >
          <p className={probe.ok ? "text-success" : "text-danger"}>
            {probe.ok
              ? t("mcp.testOk", { n: String(probe.tools.length) })
              : t("mcp.testFailed", { error: probe.error })}
          </p>
          {probe.tools.length > 0 && (
            <div className="mt-1 text-muted">
              <p>{t("mcp.testTools")}</p>
              <ul className="mt-0.5 space-y-0.5 font-mono text-[11px]">
                {probe.tools.map((tool) => (
                  <li key={tool.name} className="truncate">
                    {tool.wrapped || tool.name}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {!probe.ok && probe.error && (
            <p className="mt-1 whitespace-pre-wrap text-[11px] text-muted">{probe.error}</p>
          )}
        </div>
      )}

      {server.tools.length > 0 ? (
        <div className="mt-3 border-t border-border pt-2">
          <p className="text-[11px] text-muted">{t("mcp.toolsHint")}</p>
          <ul className="mt-1 space-y-1">
            {server.tools.map((tool) => (
              <li key={tool.name} className="flex items-start gap-2">
                <label className="flex cursor-pointer items-center gap-1.5">
                  <input
                    type="checkbox"
                    data-testid={`mcp-tool-${server.id}-${tool.name}`}
                    checked={tool.enabled}
                    onChange={(event) => toggleTool(tool.name, event.target.checked)}
                    className="h-3.5 w-3.5 accent-[var(--primary)]"
                  />
                  <span
                    className={`font-mono text-xs ${tool.enabled ? "" : "text-muted line-through"}`}
                  >
                    {tool.wrapped || tool.name}
                  </span>
                </label>
                {!tool.enabled && (
                  <span className="text-[11px] text-muted">{t("mcp.toolOff")}</span>
                )}
                {tool.description && (
                  <span className="line-clamp-1 text-[11px] text-muted">{tool.description}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="mt-3 border-t border-border pt-2 text-[11px] text-muted">
          {t("mcp.toolsEmpty")}
        </p>
      )}
    </li>
  );
}
