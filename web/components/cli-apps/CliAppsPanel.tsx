"use client";

/**
 * CLI 应用标签页（§7.18）：搜目录 → 安装/检测 → 看用法指南。
 *
 * 三件事说清楚：
 * - **装没装上以后端判定为准**（`installed` = 程序此刻真的找得到），不接受本地猜测；
 *   所以安装成功后不做乐观更新，直接重取列表——「界面说装了、其实跑不起来」最坑人。
 * - **装之前一定问一句**：pip/npm 安装要联网下载、要往应用目录写东西，是管理动作；
 *   本机自带的应用（ffmpeg/pandoc 这种）点「检测」只做一次 PATH 探测，不下载。
 * - **指南就地展开**：模型首跑时读到的就是这段文字（runner 注入），用户能自己核对
 *   「AI 以为的用法」跟真实用法对不上没有。
 */

import { useDeferredValue, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronUp,
  Download,
  ExternalLink,
  Loader2,
  RefreshCw,
  Search,
  Terminal,
  Trash2,
} from "lucide-react";
import { useCliApps, useInstallCliApp, useUninstallCliApp } from "@/hooks/useCliApps";
import { useI18n } from "@/hooks/useI18n";
import { confirmDialog } from "@/hooks/useConfirm";
import { errorText } from "@/lib/errors";
import type { CliApp } from "@/types/api";

/** 双语字段取值（口径同 skills 的 hubText）：当前语言缺了回退另一种。 */
function textIn(map: Record<string, string>, lang: string): string {
  return map[lang] || map.zh || map.en || "";
}

function statusKey(app: CliApp): "statusInstalled" | "statusUnsupported" | "statusMissing" {
  if (!app.platform_supported) {
    return "statusUnsupported";
  }
  return app.installed ? "statusInstalled" : "statusMissing";
}

const STATUS_CLASS: Record<string, string> = {
  statusInstalled: "bg-success/10 text-success",
  statusMissing: "bg-accent text-muted",
  statusUnsupported: "bg-accent text-muted",
};

const KIND_KEY: Record<string, string> = {
  pip: "kindPip",
  npm: "kindNpm",
  preinstalled: "kindPreinstalled",
  bundled: "kindBundled",
};

export default function CliAppsPanel() {
  const { t, lang } = useI18n();
  const queryClient = useQueryClient();
  const [input, setInput] = useState("");
  const [onlyInstalled, setOnlyInstalled] = useState(false);
  const query = useDeferredValue(input);
  const { data, isLoading, error } = useCliApps(query);

  const rows = data?.apps ?? [];
  const visible = onlyInstalled ? rows.filter((row) => row.installed) : rows;

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <p className="text-xs text-muted">{t("cliApps.desc")}</p>
        <button
          type="button"
          data-testid="cli-refresh"
          onClick={() => void queryClient.invalidateQueries({ queryKey: ["cli-apps"] })}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          {t("cliApps.refresh")}
        </button>
      </div>

      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            data-testid="cli-search"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={t("cliApps.searchPlaceholder")}
            className="w-full rounded-lg border border-border bg-surface py-1.5 pl-8 pr-3 text-sm outline-none focus:border-primary"
          />
        </div>
        <label className="flex shrink-0 items-center gap-1.5 text-xs text-muted">
          <input
            type="checkbox"
            data-testid="cli-only-installed"
            checked={onlyInstalled}
            onChange={(event) => setOnlyInstalled(event.target.checked)}
            className="h-3.5 w-3.5 accent-[var(--primary)]"
          />
          {t("cliApps.onlyInstalled")}
        </label>
      </div>

      {data && (
        <p data-testid="cli-count" className="text-[11px] text-muted">
          {t("cliApps.installedCount", {
            installed: String(data.installed_count),
            total: String(data.total),
          })}
          {data.catalog.commit
            ? ` · ${t("cliApps.catalogNote", { commit: data.catalog.commit.slice(0, 7) })}`
            : ""}
        </p>
      )}

      {isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
      ) : visible.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
          <Terminal className="h-6 w-6 text-muted" />
          <p className="text-sm text-muted">
            {query && !onlyInstalled ? t("cliApps.searchEmpty", { query }) : t("cliApps.empty")}
          </p>
        </div>
      ) : (
        <ul className="grid gap-2 sm:grid-cols-2">
          {visible.map((app) => (
            <CliAppCard key={app.id} app={app} lang={lang} />
          ))}
        </ul>
      )}
    </div>
  );
}

function CliAppCard({ app, lang }: { app: CliApp; lang: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const install = useInstallCliApp();
  const uninstall = useUninstallCliApp();
  const installing = install.isPending && install.variables === app.id;
  const removing = uninstall.isPending && uninstall.variables === app.id;
  const failed = install.isError && install.variables === app.id ? install.error : null;
  const status = statusKey(app);
  const selfContained = app.install.kind === "preinstalled" || app.install.kind === "bundled";

  const startInstall = async () => {
    const name = `${app.name} (${app.id})`;
    const ok = selfContained
      ? true
      : await confirmDialog({
          message: t("cliApps.installConfirm", { name }),
          confirmLabel: t("cliApps.install"),
        });
    if (ok) {
      install.mutate(app.id);
    }
  };

  const startUninstall = async () => {
    const ok = await confirmDialog({
      message: t("cliApps.uninstallConfirm", { name: `${app.name} (${app.id})` }),
      danger: true,
      confirmLabel: t("cliApps.uninstall"),
    });
    if (ok) {
      uninstall.mutate(app.id);
    }
  };

  return (
    <li
      data-testid={`cli-card-${app.id}`}
      className="flex h-full flex-col rounded-xl border border-border bg-surface p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="truncate text-sm font-medium">{app.name}</span>
        <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
          {app.category}
        </span>
        <span
          data-testid={`cli-status-${app.id}`}
          className={`rounded px-1.5 py-0.5 text-[10px] ${STATUS_CLASS[status]}`}
        >
          {t(`cliApps.${status}`)}
        </span>
        {app.network && (
          <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
            {t("cliApps.network")}
          </span>
        )}
      </div>

      <p className="mt-1 line-clamp-2 text-xs text-muted">{textIn(app.description, lang)}</p>
      <p className="mt-1 font-mono text-[10px] text-muted">
        {app.id} · {t(`cliApps.${KIND_KEY[app.install.kind] ?? "kindPreinstalled"}`)}
      </p>

      {app.installed && app.program_path && (
        <p
          data-testid={`cli-program-${app.id}`}
          className="mt-1 truncate font-mono text-[10px] text-muted"
        >
          {app.program_path}
        </p>
      )}
      {app.requires && textIn(app.requires, lang) && (
        <p className="mt-1 text-[10px] text-muted">
          {t("cliApps.requires", { text: textIn(app.requires, lang) })}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid={`cli-install-${app.id}`}
          disabled={!app.platform_supported || installing}
          onClick={() => void startInstall()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {installing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Download className="h-3.5 w-3.5" />
          )}
          {installing
            ? t("cliApps.installing")
            : selfContained
              ? t("cliApps.detect")
              : t("cliApps.install")}
        </button>
        {app.installed && !selfContained && (
          <button
            type="button"
            data-testid={`cli-uninstall-${app.id}`}
            disabled={removing}
            onClick={() => void startUninstall()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-danger transition-colors hover:bg-danger/10 disabled:opacity-50"
          >
            {removing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Trash2 className="h-3.5 w-3.5" />
            )}
            {removing ? t("cliApps.uninstalling") : t("cliApps.uninstall")}
          </button>
        )}
        <button
          type="button"
          data-testid={`cli-guide-${app.id}`}
          onClick={() => setOpen((value) => !value)}
          className="ml-auto inline-flex items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
        >
          {open ? t("cliApps.hideGuide") : t("cliApps.guide")}
          {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </button>
      </div>

      {failed && (
        <p data-testid={`cli-error-${app.id}`} className="mt-2 text-xs text-danger">
          {errorText(failed, t("cliApps.installFailed"))}
        </p>
      )}

      {open && (
        <div className="mt-3 space-y-2 border-t border-border pt-3">
          <p className="whitespace-pre-wrap text-xs">{textIn(app.guide, lang)}</p>
          <p className="text-[11px] text-muted">{t("cliApps.params")}</p>
          {Object.keys(app.params_schema.properties ?? {}).length === 0 ? (
            <p className="text-[11px] text-muted">{t("cliApps.paramsEmpty")}</p>
          ) : (
            <ul className="space-y-0.5">
              {Object.entries(app.params_schema.properties ?? {}).map(([name, spec]) => (
                <li key={name} className="text-[11px] text-muted">
                  <span className="font-mono text-foreground">{name}</span>
                  {" · "}
                  {app.params_schema.required?.includes(name)
                    ? t("cliApps.paramRequired")
                    : t("cliApps.paramOptional")}
                  {spec.description ? ` · ${spec.description}` : ""}
                </li>
              ))}
            </ul>
          )}
          <p className="font-mono text-[10px] text-muted">
            {t("cliApps.program", { program: app.entry_point })}
          </p>
          {app.install_log && (
            <div>
              <p className="text-[11px] text-muted">{t("cliApps.installLog")}</p>
              <pre
                data-testid={`cli-log-${app.id}`}
                className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap rounded border border-border bg-background p-2 font-mono text-[10px] text-muted"
              >
                {app.install_log}
              </pre>
            </div>
          )}
          {app.homepage && (
            <a
              href={app.homepage}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
            >
              {t("cliApps.homepage")}
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      )}
    </li>
  );
}
