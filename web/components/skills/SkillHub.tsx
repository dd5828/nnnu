"use client";

/**
 * 技能市场（§7.16）：内置 + 远程目录，安装前先看安全门预扫描。
 *
 * 顶部嵌的是设置区 `skills` 的表单（远程目录 URL）——它只影响市场，不该跑去设置页
 * 占一张卡片（P11 登记）。「刷新目录」自增一个令牌进查询键，带着 ?refresh=1 强刷远程。
 * 安装是快照式的：装完不跟上游同步，更新 = 重装（详情里说清楚）。
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  CheckCircle2,
  CircleAlert,
  Download,
  Loader2,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import SettingsForm from "@/components/settings/SettingsForm";
import { confirmDialog } from "@/hooks/useConfirm";
import { useI18n } from "@/hooks/useI18n";
import { useInstallSkill, useSkillHub, useSkillHubDetail } from "@/hooks/useSkills";
import { errorText } from "@/lib/errors";
import { gateCounts, hubText, ruleLabelKey } from "@/lib/skills";
import type { GateFinding } from "@/types/api";

export default function SkillHub() {
  const { t, lang } = useI18n();
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const hub = useSkillHub(refresh);

  const entries = hub.data?.entries ?? [];

  return (
    <div className="space-y-3">
      <SettingsForm
        area="skills"
        title={t("settings.skillsTitle")}
        description={t("settings.skillsDesc")}
      />

      <div className="flex items-center gap-2">
        <p className="text-xs text-muted">
          {hub.data?.hub_url ? t("skills.hubRemoteOn") : t("skills.hubRemoteOff")}
        </p>
        <button
          type="button"
          data-testid="sk-hub-refresh"
          onClick={() => setRefresh((value) => value + 1)}
          disabled={hub.isFetching}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${hub.isFetching ? "animate-spin" : ""}`} />
          {t("skills.hubRefresh")}
        </button>
      </div>

      {hub.data && !hub.data.remote_ok && (
        <p data-testid="sk-hub-remote-error" className="text-xs text-muted">
          {t("skills.hubRemoteError", { error: hub.data.remote_error ?? "" })}
        </p>
      )}

      {hub.isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : hub.error ? (
        <p className="text-sm text-danger">{errorText(hub.error, t("common.requestFailed"))}</p>
      ) : entries.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border py-12 text-center text-sm text-muted">
          {t("skills.hubEmpty")}
        </p>
      ) : (
        <ul className="grid gap-2 sm:grid-cols-2">
          {entries.map((entry) => (
            <li key={`${entry.source}-${entry.slug}`}>
              <button
                type="button"
                data-testid="sk-hub-item"
                data-slug={entry.slug}
                onClick={() => setSelected(entry.slug)}
                className="h-full w-full rounded-xl border border-border bg-surface p-4 text-left transition-colors hover:border-primary/40"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-mono text-sm font-medium">{entry.name}</span>
                  <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
                    {entry.source === "builtin"
                      ? t("skills.sourceBuiltin")
                      : t("skills.sourceRemote")}
                  </span>
                  {entry.installed && (
                    <span
                      data-testid={`sk-hub-installed-${entry.slug}`}
                      className="rounded bg-success/10 px-1.5 py-0.5 text-[10px] text-success"
                    >
                      {t("skills.hubInstalled")}
                    </span>
                  )}
                  <span className="ml-auto text-[10px] text-muted">v{entry.version}</span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-muted">
                  {hubText(entry.description, lang)}
                </p>
                <p className="mt-1 text-[10px] text-muted">
                  {t("skills.hubBy", { author: entry.author })}
                  {entry.license ? ` · ${entry.license}` : ""}
                  {` · ${t("skills.filesCount", { n: String(entry.files.length) })}`}
                </p>
              </button>
            </li>
          ))}
        </ul>
      )}

      {selected && <HubDetailDrawer slug={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function HubDetailDrawer({ slug, onClose }: { slug: string; onClose: () => void }) {
  const { t, lang } = useI18n();
  const detail = useSkillHubDetail(slug);
  const install = useInstallSkill();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  if (typeof document === "undefined") {
    return null;
  }

  const entry = detail.data;
  const counts = gateCounts(entry?.gate);

  const handleInstall = () => {
    if (!entry) {
      return;
    }
    void confirmDialog({
      message: t("skills.hubInstallConfirm", { name: entry.name }),
    }).then((ok) => {
      if (!ok) {
        return;
      }
      setError(null);
      install.mutateAsync({ slug, confirm: true }).catch((err: unknown) => {
        setError(errorText(err, t("common.requestFailed")));
      });
    });
  };

  return createPortal(
    <div
      data-testid="sk-hub-detail"
      className="fixed inset-0 z-[100] bg-background/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <aside
        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-border bg-surface"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <span className="truncate font-mono text-sm font-medium">{entry?.name ?? slug}</span>
          <button
            type="button"
            data-testid="sk-hub-detail-close"
            onClick={onClose}
            aria-label={t("common.close")}
            className="rounded-full p-1 text-muted transition-colors hover:bg-accent hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 space-y-4 overflow-auto p-4">
          {detail.isLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="h-4 w-4 animate-spin text-muted" />
            </div>
          ) : detail.error || !entry ? (
            <p className="text-sm text-danger">
              {errorText(detail.error, t("common.requestFailed"))}
            </p>
          ) : (
            <>
              <p className="whitespace-pre-wrap text-sm">{hubText(entry.description, lang)}</p>
              {hubText(entry.when_to_use, lang) && (
                <p className="text-xs text-muted">
                  {t("skills.whenToUseInline", { text: hubText(entry.when_to_use, lang) })}
                </p>
              )}

              <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="text-muted">{t("skills.hubVersion")}</dt>
                <dd className="font-mono">{entry.version}</dd>
                <dt className="text-muted">{t("skills.hubAuthor")}</dt>
                <dd>{entry.author}</dd>
                <dt className="text-muted">{t("skills.hubLicense")}</dt>
                <dd>{entry.license || "—"}</dd>
                <dt className="text-muted">{t("skills.hubSource")}</dt>
                <dd>
                  {entry.source === "builtin"
                    ? t("skills.sourceBuiltin")
                    : t("skills.sourceRemote")}
                </dd>
              </dl>
              {entry.homepage && (
                <a
                  href={entry.homepage}
                  target="_blank"
                  rel="noreferrer"
                  className="block truncate text-xs text-primary underline-offset-2 hover:underline"
                >
                  {t("skills.hubHomepage")}
                </a>
              )}

              <div>
                <div className="mb-1 text-xs font-medium">{t("skills.hubFiles")}</div>
                <ul className="space-y-0.5">
                  {entry.files.map((file) => (
                    <li key={file} className="truncate font-mono text-xs text-muted">
                      {file}
                    </li>
                  ))}
                </ul>
              </div>

              <div
                data-testid="sk-hub-gate"
                className={`rounded-lg border p-3 ${
                  counts.block > 0 ? "border-danger/40 bg-danger/5" : "border-border"
                }`}
              >
                <p
                  className={`mb-1 flex items-center gap-1.5 text-xs ${
                    counts.block > 0 ? "text-danger" : "text-success"
                  }`}
                >
                  {counts.block > 0 ? (
                    <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
                  ) : (
                    <ShieldCheck className="h-3.5 w-3.5 shrink-0" />
                  )}
                  {counts.block > 0
                    ? t("skills.gateBlocked", { n: String(counts.block) })
                    : t("skills.hubScanOk")}
                </p>
                {entry.gate.findings.length > 0 && (
                  <ul className="space-y-1">
                    {entry.gate.findings.map((finding: GateFinding, index) => (
                      <li key={`${finding.rule}-${index}`} className="text-xs">
                        <span
                          className={finding.severity === "block" ? "text-danger" : "text-muted"}
                        >
                          {t(ruleLabelKey(finding.rule))}
                        </span>
                        <span className="text-muted"> · {finding.path}</span>
                        <p className="truncate font-mono text-[10px] text-muted">
                          {finding.excerpt}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="mt-1 text-[10px] text-muted">{t("skills.hubSnapshotHint")}</p>
              </div>

              {error && (
                <p data-testid="sk-hub-install-error" className="text-xs text-danger">
                  {error}
                </p>
              )}

              <button
                type="button"
                data-testid="sk-hub-install"
                onClick={handleInstall}
                disabled={install.isPending || entry.installed || counts.block > 0}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
              >
                {install.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : entry.installed ? (
                  <CheckCircle2 className="h-3.5 w-3.5" />
                ) : counts.block > 0 ? (
                  <CircleAlert className="h-3.5 w-3.5" />
                ) : (
                  <Download className="h-3.5 w-3.5" />
                )}
                {entry.installed ? t("skills.hubInstalled") : t("skills.hubInstall")}
              </button>
            </>
          )}
        </div>
      </aside>
    </div>,
    document.body
  );
}
