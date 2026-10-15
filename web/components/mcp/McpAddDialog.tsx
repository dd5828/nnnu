"use client";

/**
 * MCP 服务器表单（§7.17）：新建（目录预填 / 手动）与编辑共用一个弹窗。
 *
 * 几处刻意的取舍：
 * - **不做「测试连接」按钮**：带密钥的保存本来就会先探一次（后端做，探不通整笔回滚），
 *   再放一个测试按钮就是两动作一件事，中间还会多出「测试通过了但保存失败」这一幕。
 *   已经存下来的服务器要复测，卡片上有「测试连接」。
 * - **密钥输入永远是空框**：后端只有掩码，值不回显；留空 = 不改这个字段，要换就整个换。
 * - **目录条目只是预填**：点一下把地址/安装方式/需要的字段摆好，密钥还得自己贴。
 *   字段的落点（请求头 / URL 查询串）记在那一行里，保存时按引用写进配置——
 *   明文只走 secrets，配置里留下的永远是 `${secret:字段}`。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ExternalLink, Loader2, Server } from "lucide-react";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useI18n } from "@/hooks/useI18n";
import { useCreateMcpServer, useMcpCatalog, useUpdateMcpServer } from "@/hooks/useMcp";
import { errorText } from "@/lib/errors";
import type { McpCatalogEntry, McpServer, McpServerInput } from "@/types/api";

interface Props {
  /** 编辑时传已存的服务器；新建传 null。 */
  server: McpServer | null;
  onClose: () => void;
}

const TRANSPORTS = [
  { value: "streamable_http", label: "mcp.transportHttp" },
  { value: "stdio", label: "mcp.transportStdio" },
  { value: "sse", label: "mcp.transportSse" },
] as const;

/** 一个凭据字段（目录带出来的，或用户手动加的）。 */
interface SecretRow {
  key: string;
  value: string;
  label?: string;
  placeholder?: string;
  /** 目录字段才知道落点：进请求头还是进 URL 查询串。手动加的字段两头都不进（只存值）。 */
  target?: "header" | "query";
  name?: string;
  template?: string;
}

function secretRef(key: string): string {
  return `\${secret:${key}}`;
}

/** 查询参数拼字面量：值本来就是 `${secret:x}` 引用，编码由后端重新做（见 secrets.py）。 */
function withQueryParam(url: string, name: string, value: string): string {
  if (!url) {
    return url;
  }
  return `${url}${url.includes("?") ? "&" : "?"}${name}=${value}`;
}

/** 参数一行一个：比按空格切好——路径里带空格、参数里带空格，切成什么全看运气。 */
function splitArgs(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export default function McpAddDialog({ server, onClose }: Props) {
  const { t, lang } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef, true);

  const editing = server !== null;
  const create = useCreateMcpServer();
  const update = useUpdateMcpServer();
  const catalog = useMcpCatalog();
  const [mode, setMode] = useState<"catalog" | "manual">(editing ? "manual" : "catalog");
  const [entry, setEntry] = useState(server?.catalog_entry ?? "");

  const [id, setId] = useState(server?.id ?? "");
  const [name, setName] = useState(server?.name ?? "");
  const [transport, setTransport] = useState(server?.transport || "streamable_http");
  const [url, setUrl] = useState(server?.url ?? "");
  const [command, setCommand] = useState(server?.command ?? "");
  const [args, setArgs] = useState((server?.args ?? []).join("\n"));
  const [rows, setRows] = useState<SecretRow[]>(() => {
    const known = new Set(Object.keys(server?.secret_fields ?? {}));
    for (const key of server?.credential?.secret_names ?? []) {
      known.add(key);
    }
    // 已配好的字段先摆出来（值留空 = 不改），末尾留一个空行给新字段
    return [...known].map((key) => ({ key, value: "" })).concat({ key: "", value: "" });
  });

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pending) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, pending]);

  // 保存成功才关：失败要留在原地（连不上的原因就写在这儿，关了等于把话吞了）
  const saved = create.isSuccess || update.isSuccess;
  useEffect(() => {
    if (saved) {
      onClose();
    }
  }, [saved, onClose]);

  const isStdio = transport === "stdio";
  const filledRows = rows.filter((row) => row.key.trim());

  const applyEntry = (item: McpCatalogEntry) => {
    setEntry(item.id);
    setId(item.id);
    setName(item.display_name || item.name);
    setTransport(item.transport || "streamable_http");
    setUrl(item.url ?? "");
    setCommand(item.command ?? "");
    setArgs((item.args ?? []).join("\n"));
    setRows([
      ...item.fields.map((field) => ({
        key: field.key,
        value: "",
        label: field.label?.[lang] || field.key,
        placeholder: field.placeholder,
        target: field.target,
        name: field.name,
        template: field.value_template,
      })),
      { key: "", value: "" },
    ]);
    setMode("manual");
  };

  const submit = () => {
    // 值进密钥库；配置里只留引用。留空的行 = 不动这个字段（密钥不回显，没法「看到再改」）
    const secrets: Record<string, string> = {};
    for (const row of filledRows) {
      if (row.value) {
        secrets[row.key.trim()] = row.value;
      }
    }
    // 目录字段按落点写进 headers / URL：写的都是引用，真值只在连接时从密钥库换出来
    const headers: Record<string, string> = { ...(server?.headers ?? {}) };
    let finalUrl = url.trim();
    const declared: string[] = [];
    for (const row of filledRows) {
      if (!row.target) {
        continue;
      }
      const key = row.key.trim();
      declared.push(key);
      const value =
        row.template && row.template.includes("${secret:") ? row.template : secretRef(key);
      if (row.target === "header") {
        headers[row.name || key] = value;
      } else {
        finalUrl = withQueryParam(finalUrl, row.name || key, value);
      }
    }

    const body: McpServerInput = {
      name: name.trim(),
      transport,
      url: finalUrl,
      command: command.trim(),
      args: splitArgs(args),
      catalog_entry: entry,
      ...(Object.keys(headers).length ? { headers } : {}),
      ...(declared.length
        ? {
            credential: {
              kind: "apikey" as const,
              header: "",
              value_template: "",
              secret_names: declared,
            },
          }
        : {}),
      ...(Object.keys(secrets).length ? { secrets } : {}),
    };
    if (editing && server) {
      update.mutate({ id: server.id, ...body });
    } else {
      create.mutate({ id: id.trim(), ...body });
    }
  };

  const detail = isStdio ? command.trim() : url.trim();
  const canSave = Boolean(detail) && (editing || Boolean(id.trim()));

  const entries = useMemo(() => catalog.data?.entries ?? [], [catalog.data]);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4">
      <div
        className="absolute inset-0 bg-black/40"
        onClick={() => !pending && onClose()}
        aria-hidden
      />
      <div
        ref={dialogRef}
        data-testid="mcp-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={editing ? t("mcp.editTitle") : t("mcp.addTitle")}
        className="relative my-4 w-full max-w-2xl rounded-xl border border-border bg-surface p-5 shadow-lg"
      >
        <h2 className="text-sm font-semibold">
          {editing ? t("mcp.editTitle") : t("mcp.addTitle")}
        </h2>

        {mode === "catalog" ? (
          <div className="mt-3 space-y-2">
            <p className="text-xs text-muted">{t("mcp.catalogHint")}</p>
            {catalog.isLoading ? (
              <div className="flex justify-center py-6">
                <Loader2 className="h-4 w-4 animate-spin text-muted" />
              </div>
            ) : (
              <ul className="max-h-80 space-y-1.5 overflow-y-auto pr-1">
                {entries.map((item) => {
                  const requirement = item.requires?.[lang];
                  return (
                    <li key={item.id}>
                      <button
                        type="button"
                        data-testid={`mcp-catalog-${item.id}`}
                        onClick={() => applyEntry(item)}
                        className="w-full rounded-lg border border-border bg-background p-3 text-left transition-colors hover:border-primary/40"
                      >
                        <div className="flex items-center gap-2">
                          <Server className="h-3.5 w-3.5 shrink-0 text-muted" />
                          <span className="truncate text-sm font-medium">
                            {item.display_name || item.name}
                          </span>
                          <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
                            {item.transport === "stdio"
                              ? t("mcp.transportStdio")
                              : item.transport === "sse"
                                ? t("mcp.transportSse")
                                : t("mcp.transportHttp")}
                          </span>
                          {item.homepage && (
                            <span className="ml-auto inline-flex shrink-0 items-center gap-0.5 text-[10px] text-muted">
                              <ExternalLink className="h-3 w-3" />
                              {t("mcp.catalogHomepage")}
                            </span>
                          )}
                        </div>
                        <p className="mt-1 line-clamp-2 text-xs text-muted">
                          {item.description?.[lang] ?? ""}
                        </p>
                        {requirement && (
                          <p className="mt-1 text-[11px] text-muted">
                            {t("mcp.catalogRequired", { text: requirement })}
                          </p>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
            <div className="flex items-center justify-between pt-1">
              <button
                type="button"
                data-testid="mcp-mode-manual"
                onClick={() => setMode("manual")}
                className="rounded-lg px-3 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
              >
                {t("mcp.manual")}
              </button>
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-border px-3.5 py-1.5 text-sm transition-colors hover:bg-accent"
              >
                {t("common.cancel")}
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-3 space-y-3">
            {!editing && (
              <button
                type="button"
                data-testid="mcp-mode-catalog"
                onClick={() => setMode("catalog")}
                className="inline-flex items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
              >
                <ArrowLeft className="h-3 w-3" />
                {t("mcp.fromCatalog")}
              </button>
            )}

            <div className="grid grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-muted">{t("mcp.id")}</span>
                <input
                  data-testid="mcp-field-id"
                  value={id}
                  onChange={(event) => setId(event.target.value)}
                  disabled={editing}
                  placeholder={t("mcp.idPlaceholder")}
                  className="w-full rounded-lg border border-border bg-background px-2.5 py-1.5 font-mono text-sm disabled:opacity-60"
                />
                {editing && <span className="block text-[11px] text-muted">{t("mcp.idHint")}</span>}
              </label>
              <label className="space-y-1">
                <span className="text-xs text-muted">{t("mcp.name")}</span>
                <input
                  data-testid="mcp-field-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder={t("mcp.namePlaceholder")}
                  className="w-full rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm"
                />
              </label>
            </div>

            <div className="space-y-3 rounded-lg border border-border bg-background p-3">
              <div className="flex items-center gap-1">
                {TRANSPORTS.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    data-testid={`mcp-field-transport-${item.value}`}
                    onClick={() => setTransport(item.value)}
                    className={`rounded-lg px-2.5 py-1 text-xs transition-colors ${
                      transport === item.value
                        ? "bg-accent font-medium text-foreground"
                        : "text-muted hover:bg-accent/60"
                    }`}
                  >
                    {t(item.label)}
                  </button>
                ))}
              </div>

              {isStdio ? (
                <div className="space-y-1">
                  <span className="text-xs text-muted">{t("mcp.command")}</span>
                  <input
                    data-testid="mcp-field-command"
                    value={command}
                    onChange={(event) => setCommand(event.target.value)}
                    placeholder={t("mcp.commandPlaceholder")}
                    className="w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 font-mono text-xs"
                  />
                  <textarea
                    data-testid="mcp-field-args"
                    value={args}
                    onChange={(event) => setArgs(event.target.value)}
                    rows={2}
                    aria-label={t("mcp.args")}
                    placeholder={t("mcp.argsPlaceholder")}
                    className="w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 font-mono text-xs"
                  />
                  <span className="block text-[11px] text-muted">{t("mcp.commandHint")}</span>
                </div>
              ) : (
                <label className="block space-y-1">
                  <span className="text-xs text-muted">{t("mcp.url")}</span>
                  <input
                    data-testid="mcp-field-url"
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    placeholder={t("mcp.urlPlaceholder")}
                    className="w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 font-mono text-xs"
                  />
                  <span className="block text-[11px] text-muted">{t("mcp.commandHint")}</span>
                </label>
              )}
            </div>

            <div className="space-y-2 rounded-lg border border-border bg-background p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium">{t("mcp.secrets")}</span>
                <span className="text-[11px] text-muted">{t("mcp.secretsHint")}</span>
              </div>
              {rows.map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <input
                    data-testid={`mcp-secret-key-${index}`}
                    value={row.key}
                    onChange={(event) =>
                      setRows(
                        rows.map((it, i) => (i === index ? { ...it, key: event.target.value } : it))
                      )
                    }
                    aria-label={t("mcp.secretName")}
                    placeholder={row.label ?? t("mcp.secretName")}
                    className="w-40 rounded-lg border border-border bg-surface px-2 py-1 font-mono text-xs"
                  />
                  <input
                    data-testid={`mcp-secret-value-${index}`}
                    type="password"
                    value={row.value}
                    onChange={(event) =>
                      setRows(
                        rows.map((it, i) =>
                          i === index ? { ...it, value: event.target.value } : it
                        )
                      )
                    }
                    aria-label={t("mcp.secretValue")}
                    placeholder={row.placeholder || t("mcp.secretKeep")}
                    className="flex-1 rounded-lg border border-border bg-surface px-2 py-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    title={t("mcp.secretRemove")}
                    aria-label={t("mcp.secretRemove")}
                    onClick={() => setRows(rows.filter((_row, i) => i !== index))}
                    className="rounded-lg px-2 py-1 text-xs text-muted transition-colors hover:bg-accent hover:text-danger"
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                type="button"
                data-testid="mcp-secret-add"
                onClick={() => setRows([...rows, { key: "", value: "" }])}
                className="rounded-lg border border-dashed border-border px-2.5 py-1 text-xs text-muted transition-colors hover:border-primary/40 hover:text-foreground"
              >
                {t("mcp.secretAdd")}
              </button>
            </div>

            {error && (
              <p data-testid="mcp-dialog-error" className="text-xs whitespace-pre-wrap text-danger">
                {errorText(error, t("common.requestFailed"))}
              </p>
            )}

            <div className="flex items-center gap-2 pt-1">
              <span className="text-[11px] text-muted">{t("mcp.saveHint")}</span>
              <button
                type="button"
                onClick={onClose}
                className="ml-auto shrink-0 rounded-lg border border-border px-3.5 py-1.5 text-sm transition-colors hover:bg-accent"
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                data-testid="mcp-save"
                disabled={!canSave || pending}
                onClick={submit}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
              >
                {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {pending ? t("mcp.saving") : t("mcp.save")}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
