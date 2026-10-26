"use client";

/**
 * GitHub 源（§7.9 P14）：一个知识库可以绑多个仓库/分支/glob。
 *
 * 「检查」只看远端漂移不改文档，「同步」按差异增改删。改库后点一次同步，
 * 新内容就进索引——这是 P14 验收里的「重新同步拉取到新内容」。
 */

import { useState } from "react";
import { Github, Loader2, RefreshCw, Trash2, Radar } from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import {
  useAddGithubSource,
  useCheckGithubSource,
  useDeleteGithubSource,
  useGithubSources,
  useSyncGithubSource,
} from "@/hooks/useGithubSources";
import { useI18n } from "@/hooks/useI18n";
import { errorText } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { SourceSyncReport } from "@/types/api";

function ReportLine({ report }: { report: SourceSyncReport }) {
  const { t } = useI18n();
  return (
    <p data-testid="github-sync-report" className="mt-1 text-xs text-muted">
      {t("knowledge.githubReport", {
        added: String(report.added.length),
        modified: String(report.modified.length),
        deleted: String(report.deleted.length),
        docs: String(report.docs_created + report.docs_updated + report.docs_deleted),
      })}
      {report.failed.length > 0
        ? ` · ${t("knowledge.githubReportFailed", { n: String(report.failed.length) })}`
        : ""}
    </p>
  );
}

export default function GithubSourcesPanel({ kbId }: { kbId: string }) {
  const { t, lang } = useI18n();
  const { data, isLoading, error } = useGithubSources(kbId);
  const add = useAddGithubSource(kbId);
  const remove = useDeleteGithubSource(kbId);
  const check = useCheckGithubSource(kbId);
  const sync = useSyncGithubSource(kbId);
  const [formOpen, setFormOpen] = useState(false);
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("main");
  const [glob, setGlob] = useState("**/*.md");
  const [interval, setInterval] = useState("60");
  const [autoSync, setAutoSync] = useState(true);
  const [reports, setReports] = useState<Record<string, SourceSyncReport>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const sources = data?.sources ?? [];

  const submit = async () => {
    const trimmedRepo = repo.trim();
    if (!trimmedRepo) {
      setFormError(t("knowledge.githubRepoRequired"));
      return;
    }
    setFormError(null);
    try {
      const result = await add.mutateAsync({
        repo: trimmedRepo,
        branch: branch.trim() || "main",
        glob: glob.trim() || "**/*.md",
        auto_sync: autoSync,
        interval_minutes: Math.max(1, Number(interval) || 60),
      });
      setRepo("");
      setFormOpen(false);
      if (result.sync) {
        setReports((prev) => ({
          ...prev,
          [result.source.source_id]: result.sync as SourceSyncReport,
        }));
      }
      if (result.sync_error) {
        setRowError(result.sync_error);
      }
    } catch (addError) {
      setFormError(errorText(addError, t("common.requestFailed")));
    }
  };

  const runCheck = async (sourceId: string) => {
    setRowError(null);
    try {
      const report = await check.mutateAsync(sourceId);
      setReports((prev) => ({ ...prev, [sourceId]: report }));
    } catch (checkError) {
      setRowError(errorText(checkError, t("common.requestFailed")));
    }
  };

  const runSync = async (sourceId: string) => {
    setRowError(null);
    try {
      const report = await sync.mutateAsync(sourceId);
      setReports((prev) => ({ ...prev, [sourceId]: report }));
    } catch (syncError) {
      setRowError(errorText(syncError, t("common.requestFailed")));
    }
  };

  const removeSource = (sourceId: string, name: string) => {
    void confirmDialog({
      message: t("knowledge.githubDeleteConfirm", { name }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        remove.mutate(sourceId);
      }
    });
  };

  return (
    <section
      data-testid="github-sources"
      className="rounded-xl border border-border bg-surface p-5"
    >
      <div className="mb-3 flex items-center gap-2">
        <Github className="h-4 w-4 text-primary" />
        <h2 className="text-base font-semibold">{t("knowledge.githubTitle")}</h2>
        <span className="text-xs text-muted">{t("knowledge.githubDesc")}</span>
        <button
          type="button"
          data-testid="github-add"
          onClick={() => setFormOpen((open) => !open)}
          className="ml-auto rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent"
        >
          {formOpen ? t("knowledge.cancel") : t("knowledge.githubAdd")}
        </button>
      </div>

      {formOpen && (
        <div className="mb-3 space-y-2 rounded-lg bg-accent/40 p-3">
          <div className="grid gap-2 sm:grid-cols-2">
            <input
              data-testid="github-repo"
              value={repo}
              onChange={(event) => {
                setRepo(event.target.value);
                setFormError(null);
              }}
              placeholder={t("knowledge.githubRepoPlaceholder")}
              className="rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/60"
            />
            <input
              value={branch}
              onChange={(event) => setBranch(event.target.value)}
              placeholder={t("knowledge.githubBranch")}
              className="rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/60"
            />
            <input
              value={glob}
              onChange={(event) => setGlob(event.target.value)}
              placeholder={t("knowledge.githubGlob")}
              className="rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/60"
            />
            <label className="flex items-center gap-2 text-sm text-muted">
              <input
                type="checkbox"
                checked={autoSync}
                onChange={(event) => setAutoSync(event.target.checked)}
              />
              {t("knowledge.githubAutoSync")}
              <input
                type="number"
                min={1}
                max={1440}
                value={interval}
                disabled={!autoSync}
                onChange={(event) => setInterval(event.target.value)}
                className="w-16 rounded-lg border border-border bg-background/40 px-2 py-1 text-sm outline-none focus:border-primary/60 disabled:opacity-50"
              />
              {t("knowledge.githubMinutes")}
            </label>
          </div>
          {formError && <p className="text-xs text-danger">{formError}</p>}
          <button
            type="button"
            data-testid="github-add-submit"
            disabled={add.isPending}
            onClick={() => void submit()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
          >
            {add.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {add.isPending ? t("knowledge.githubAdding") : t("knowledge.githubAddSubmit")}
          </button>
        </div>
      )}

      {isLoading ? (
        <div className="flex justify-center py-4">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
      ) : sources.length === 0 ? (
        <p className="text-sm text-muted">{t("knowledge.githubEmpty")}</p>
      ) : (
        <ul className="space-y-2">
          {sources.map((source) => (
            <li
              key={source.source_id}
              data-testid="github-source"
              className="rounded-lg border border-border/70 p-3"
            >
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">
                    {source.repo}
                    <span className="ml-1 text-xs text-muted">
                      @ {source.branch} · {source.glob}
                    </span>
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    {t("knowledge.githubSynced", { n: String(Object.keys(source.docs).length) })}
                    {" · "}
                    {t("knowledge.githubLastSync", {
                      time: formatDateTime(source.last_sync_at, lang) || "—",
                    })}
                    {" · "}
                    {source.auto_sync
                      ? t("knowledge.githubEvery", { n: String(source.interval_minutes) })
                      : t("knowledge.githubManual")}
                  </p>
                  {source.last_error && (
                    <p className="mt-0.5 text-xs text-danger">{source.last_error}</p>
                  )}
                  {reports[source.source_id] && <ReportLine report={reports[source.source_id]} />}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    title={t("knowledge.githubCheck")}
                    onClick={() => void runCheck(source.source_id)}
                    disabled={check.isPending}
                    className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent disabled:opacity-50"
                  >
                    <Radar className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    title={t("knowledge.githubSync")}
                    onClick={() => void runSync(source.source_id)}
                    disabled={sync.isPending}
                    className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent disabled:opacity-50"
                  >
                    {sync.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <RefreshCw className="h-4 w-4" />
                    )}
                  </button>
                  <button
                    type="button"
                    title={t("knowledge.githubRemove")}
                    onClick={() => removeSource(source.source_id, source.repo)}
                    disabled={remove.isPending}
                    className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger disabled:opacity-50"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {rowError && <p className="mt-2 text-xs text-danger">{rowError}</p>}
    </section>
  );
}
