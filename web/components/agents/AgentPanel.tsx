"use client";

/**
 * 子代理「连接」标签页（§7.12）：建/看/探/删本机编码 CLI 的连接。
 *
 * 三条界面口径，都来自后端的硬规矩：
 *
 * - **探测以真跑为准**。后端起一个进程跑 `<程序> --version` 看它退不退 0，所以结果
 *   只在点的那一刻成立：显示成一次性消息（"刚才那次：不可用 · …"），不做缓存徽章。
 * - **建连接必须勾知情**。无头运行没有交互确认，子代理以本用户权限直接在 cwd 里读写
 *   文件——这是唯一一次知情机会，不勾就不给建（后端也会 400 挡回来）。
 * - **「能探到」≠「能用」**。本批只接上了 Claude Code 一条链路，其余五种 kind 建得出来、
 *   探得到（说明本机装了没），但咨询会明确拒绝——所以卡片上「本版本已接入/先只记着」
 *   两个态必须分开显示，免得用户以为探到就走得通。
 */

import { useState } from "react";
import { Bot, Loader2, Plug, Plus, Radar, ShieldAlert, Trash2 } from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import { useI18n } from "@/hooks/useI18n";
import {
  useAgentConnections,
  useCreateAgentConnection,
  useDeleteAgentConnection,
  useProbeAgentConnection,
} from "@/hooks/useAgents";
import { errorText } from "@/lib/errors";
import type { AgentConnection, AgentKindMeta, AgentProbe } from "@/types/api";

/** 各 kind 的显示名（没有译文就退回后端给的 kind 本身，别显示成空白）。 */
const KIND_KEY: Record<string, string> = {
  claude_code: "kindClaudeCode",
  codex: "kindCodex",
  gemini: "kindGemini",
  kimi: "kindKimi",
  opencode: "kindOpencode",
  mimo: "kindMimo",
};

const PERMISSION_KEY: Record<string, string> = {
  bypassPermissions: "permBypass",
  acceptEdits: "permAcceptEdits",
  plan: "permPlan",
  default: "permDefault",
};

export default function AgentPanel() {
  const { t } = useI18n();
  const { data, isLoading, error } = useAgentConnections();
  const [creating, setCreating] = useState(false);
  const connections = data?.connections ?? [];

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <p className="text-xs text-muted">{t("agents.desc")}</p>
        <button
          type="button"
          data-testid="ag-new"
          onClick={() => setCreating((value) => !value)}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("agents.newConnection")}
        </button>
      </div>

      {creating && data && (
        <NewConnectionForm
          kinds={data.kinds}
          permissionModes={data.permission_modes}
          customAllowed={data.custom_command_allowed}
          onDone={() => setCreating(false)}
        />
      )}

      {isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
      ) : connections.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
          <Plug className="h-6 w-6 text-muted" />
          <p className="max-w-md text-sm text-muted">{t("agents.empty")}</p>
        </div>
      ) : (
        <ul className="grid gap-2">
          {connections.map((connection) => (
            <ConnectionCard key={connection.id} connection={connection} />
          ))}
        </ul>
      )}
    </div>
  );
}

function NewConnectionForm({
  kinds,
  permissionModes,
  customAllowed,
  onDone,
}: {
  kinds: AgentKindMeta[];
  permissionModes: string[];
  customAllowed: boolean;
  onDone: () => void;
}) {
  const { t } = useI18n();
  const create = useCreateAgentConnection();
  const [kind, setKind] = useState(kinds[0]?.kind ?? "claude_code");
  const [name, setName] = useState("");
  const [cwd, setCwd] = useState("");
  const [model, setModel] = useState("");
  const [permission, setPermission] = useState(permissionModes[0] ?? "bypassPermissions");
  const [command, setCommand] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);

  const selected = kinds.find((item) => item.kind === kind);
  const submit = () => {
    const parts = command
      .split(/\s+/)
      .map((part) => part.trim())
      .filter(Boolean);
    create.mutate(
      {
        kind,
        display_name: name,
        cwd,
        model_override: model,
        permission_mode: permission,
        command: parts,
        acknowledged,
      },
      { onSuccess: () => onDone() }
    );
  };

  const field =
    "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-primary";

  return (
    <div className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className="text-xs text-muted">{t("agents.fieldKind")}</span>
          <select
            data-testid="ag-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className={field}
          >
            {kinds.map((item) => (
              <option key={item.kind} value={item.kind}>
                {KIND_KEY[item.kind] ? t(`agents.${KIND_KEY[item.kind]}`) : item.kind}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">{t("agents.fieldName")}</span>
          <input
            data-testid="ag-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t("agents.fieldNamePlaceholder")}
            className={field}
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">{t("agents.fieldCwd")}</span>
          <input
            data-testid="ag-cwd"
            value={cwd}
            onChange={(event) => setCwd(event.target.value)}
            placeholder={t("agents.fieldCwdPlaceholder")}
            className={`${field} font-mono text-xs`}
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">{t("agents.fieldModel")}</span>
          <input
            data-testid="ag-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder={t("agents.fieldModelPlaceholder")}
            className={field}
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-muted">{t("agents.fieldPermission")}</span>
          <select
            data-testid="ag-permission"
            value={permission}
            onChange={(event) => setPermission(event.target.value)}
            className={field}
          >
            {permissionModes.map((mode) => (
              <option key={mode} value={mode}>
                {PERMISSION_KEY[mode] ? t(`agents.${PERMISSION_KEY[mode]}`) : mode}
              </option>
            ))}
          </select>
        </label>
        {customAllowed && (
          <label className="space-y-1">
            <span className="text-xs text-muted">{t("agents.fieldCommand")}</span>
            <input
              data-testid="ag-command"
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder={selected?.default_command.join(" ") ?? "claude"}
              className={`${field} font-mono text-xs`}
            />
          </label>
        )}
      </div>

      <ul className="space-y-1 text-[11px] text-muted">
        <li>
          {t("agents.defaultCommand", {
            command: selected?.default_command.join(" ") ?? "",
          })}
        </li>
        {selected && !selected.supported && (
          <li data-testid="ag-kind-unsupported" className="text-danger">
            {t("agents.kindNotWired")}
          </li>
        )}
      </ul>

      <label className="flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/5 p-2.5 text-[11px]">
        <input
          type="checkbox"
          data-testid="ag-ack"
          checked={acknowledged}
          onChange={(event) => setAcknowledged(event.target.checked)}
          className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[var(--primary)]"
        />
        <span className="flex items-start gap-1.5">
          <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0 text-danger" />
          {t("agents.ack")}
        </span>
      </label>

      {create.isError && (
        <p data-testid="ag-create-error" className="text-xs text-danger">
          {errorText(create.error, t("agents.createFailed"))}
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="ag-create"
          disabled={!acknowledged || create.isPending}
          onClick={submit}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {create.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Plus className="h-3.5 w-3.5" />
          )}
          {create.isPending ? t("agents.creating") : t("agents.create")}
        </button>
        <button
          type="button"
          data-testid="ag-cancel"
          onClick={onDone}
          className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent"
        >
          {t("common.cancel")}
        </button>
      </div>
    </div>
  );
}

function ConnectionCard({ connection }: { connection: AgentConnection }) {
  const { t } = useI18n();
  const probe = useProbeAgentConnection();
  const remove = useDeleteAgentConnection();
  const [result, setResult] = useState<AgentProbe | null>(null);

  const startProbe = () => {
    // 上一次的结果先清掉：探的是"此刻"，留着旧结论会让人以为刚探过
    setResult(null);
    probe.mutate(connection.id, { onSuccess: (data) => setResult(data) });
  };

  const startDelete = async () => {
    const ok = await confirmDialog({
      message: t("agents.deleteConfirm", { name: connection.label }),
      danger: true,
      confirmLabel: t("agents.delete"),
    });
    if (ok) {
      remove.mutate(connection.id);
    }
  };

  return (
    <li
      data-testid={`ag-card-${connection.id}`}
      className="flex h-full flex-col rounded-xl border border-border bg-surface p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Bot className="h-3.5 w-3.5 shrink-0 text-muted" />
        <span className="truncate text-sm font-medium">{connection.label}</span>
        <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
          {KIND_KEY[connection.kind] ? t(`agents.${KIND_KEY[connection.kind]}`) : connection.kind}
        </span>
        <span
          data-testid={`ag-wired-${connection.id}`}
          className={`rounded px-1.5 py-0.5 text-[10px] ${
            connection.supported ? "bg-success/10 text-success" : "bg-accent text-muted"
          }`}
        >
          {connection.supported ? t("agents.wired") : t("agents.notWired")}
        </span>
      </div>

      <p className="mt-1 truncate font-mono text-[10px] text-muted" title={connection.cwd}>
        {t("agents.cwdLine", { cwd: connection.cwd })}
      </p>
      <p className="mt-1 font-mono text-[10px] text-muted">
        {t("agents.argvLine", {
          argv: connection.argv.join(" "),
          mode: PERMISSION_KEY[connection.permission_mode]
            ? t(`agents.${PERMISSION_KEY[connection.permission_mode]}`)
            : connection.permission_mode,
        })}
      </p>
      {connection.model_override && (
        <p className="mt-1 font-mono text-[10px] text-muted">
          {t("agents.modelLine", { model: connection.model_override })}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid={`ag-probe-${connection.id}`}
          disabled={probe.isPending}
          onClick={startProbe}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs transition-colors hover:bg-accent disabled:opacity-50"
        >
          {probe.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Radar className="h-3.5 w-3.5" />
          )}
          {probe.isPending ? t("agents.probing") : t("agents.probe")}
        </button>
        <button
          type="button"
          data-testid={`ag-delete-${connection.id}`}
          disabled={remove.isPending}
          onClick={() => void startDelete()}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-danger transition-colors hover:bg-danger/10 disabled:opacity-50"
        >
          {remove.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Trash2 className="h-3.5 w-3.5" />
          )}
          {t("agents.delete")}
        </button>
      </div>

      {result && (
        <p
          data-testid={`ag-probe-result-${connection.id}`}
          data-available={result.available}
          className={`mt-2 text-xs ${result.available ? "text-success" : "text-danger"}`}
        >
          {result.available ? t("agents.probeOk") : t("agents.probeFailed")}
          {result.detail ? ` · ${result.detail}` : ""}
        </p>
      )}
      {probe.isError && (
        <p data-testid={`ag-probe-error-${connection.id}`} className="mt-2 text-xs text-danger">
          {errorText(probe.error, t("agents.probeFailed"))}
        </p>
      )}
      {remove.isError && (
        <p className="mt-2 text-xs text-danger">
          {errorText(remove.error, t("common.requestFailed"))}
        </p>
      )}
    </li>
  );
}
