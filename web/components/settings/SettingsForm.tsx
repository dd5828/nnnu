"use client";

/** spec 驱动的设置表单（§7.19）：字段自动渲染 + 草稿-应用两段式；models 区附探测。 */

import { useEffect, useId, useMemo, useState } from "react";
import { CheckCircle2, CircleAlert, Loader2, PlugZap, XCircle } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useSettingsArea } from "@/hooks/useSettings";
import { useI18n } from "@/hooks/useI18n";
import type { SettingsAreaResponse, SettingsFieldMeta } from "@/types/api";

interface SettingsFormProps {
  area: string;
  title: string;
  description?: string;
}

interface ProbeResponse {
  ok: boolean;
  models: string[];
  error: string | null;
}

type FormStatus = { kind: "idle" } | { kind: "busy" } | { kind: "ok"; message: string } | { kind: "error"; message: string };

export default function SettingsForm({ area, title, description }: SettingsFormProps) {
  const { t } = useI18n();
  const { data, isLoading } = useSettingsArea(area);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [draftLoaded, setDraftLoaded] = useState(false);
  const [clearSecrets, setClearSecrets] = useState<Set<string>>(new Set());
  const [status, setStatus] = useState<FormStatus>({ kind: "idle" });
  const [probe, setProbe] = useState<ProbeResponse | null>(null);
  const [probing, setProbing] = useState(false);

  const fields: SettingsFieldMeta[] = useMemo(() => data?.fields ?? [], [data]);

  // 初始化表单：有草稿先回填草稿（提示"有未应用改动"），否则用当前值
  useEffect(() => {
    if (!data || draftLoaded) {
      return;
    }
    void (async () => {
      try {
        const draft = await apiFetch<{ draft: Record<string, unknown> | null }>(
          `/api/v1/settings/${area}/draft`
        );
        if (draft?.draft && Object.keys(draft.draft).length > 0) {
          setForm({ ...data.values, ...draft.draft });
        } else {
          setForm({ ...data.values });
        }
      } catch {
        setForm({ ...data.values }); // 无草稿（404）
      } finally {
        setDraftLoaded(true);
      }
    })();
  }, [data, area, draftLoaded]);

  const setField = (key: string, value: unknown) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setStatus({ kind: "idle" });
  };

  const buildPayload = () => {
    const payload: Record<string, unknown> = { ...form };
    for (const key of clearSecrets) {
      payload[key] = "clear";
    }
    return payload;
  };

  const handleSaveDraft = async () => {
    setStatus({ kind: "busy" });
    try {
      await apiFetch(`/api/v1/settings/${area}/draft`, {
        method: "PUT",
        body: JSON.stringify({ values: buildPayload() }),
      });
      setStatus({ kind: "ok", message: t("settings.draftSaved") });
      setClearSecrets(new Set());
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  };

  const handleApply = async () => {
    setStatus({ kind: "busy" });
    setProbe(null);
    try {
      // 一键应用 = 先存草稿再 apply（§7.19 草稿-应用；探测失败时草稿保留可重试）
      await apiFetch(`/api/v1/settings/${area}/draft`, {
        method: "PUT",
        body: JSON.stringify({ values: buildPayload() }),
      });
      await apiFetch(`/api/v1/settings/${area}/apply`, { method: "POST" });
      setStatus({ kind: "ok", message: t("settings.applied") });
      setClearSecrets(new Set());
      // 重取当前值，表单同步到已应用状态
      const fresh = await apiFetch<SettingsAreaResponse>(`/api/v1/settings/${area}`);
      setForm({ ...fresh.values });
    } catch (error) {
      const message = String(error);
      setStatus({ kind: "error", message });
      // 探测失败时把详情带出来（§7.19：失败回滚并提示）
      if (message.includes("probe_failed")) {
        setProbe({ ok: false, models: [], error: message.replace(/.*probe_failed[：:]\s*/, "") });
      }
    }
  };

  const handleDiscard = async () => {
    await apiFetch(`/api/v1/settings/${area}/draft`, { method: "DELETE" }).catch(() => undefined);
    const fresh = await apiFetch<SettingsAreaResponse>(`/api/v1/settings/${area}`);
    setForm({ ...fresh.values });
    setClearSecrets(new Set());
    setStatus({ kind: "idle" });
    setProbe(null);
  };

  const handleProbe = async () => {
    setProbing(true);
    setProbe(null);
    try {
      const body: Record<string, unknown> = {
        provider: form.provider ?? "",
        base_url: form.base_url ?? "",
        model: form.model ?? "",
      };
      for (const field of fields) {
        if (field.type === "secret" && typeof form[field.key] === "string" && form[field.key]) {
          body.api_key = form[field.key];
        }
      }
      const result = await apiFetch<ProbeResponse>("/api/v1/settings/probe", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setProbe(result);
    } catch (error) {
      setProbe({ ok: false, models: [], error: String(error) });
    } finally {
      setProbing(false);
    }
  };

  if (isLoading || !draftLoaded) {
    return (
      <div className="rounded-xl border border-border bg-surface p-5">
        <Loader2 className="h-4 w-4 animate-spin text-muted" />
      </div>
    );
  }

  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="mb-4">
        <h2 className="text-base font-semibold">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      <div className="space-y-4">
        {fields.map((field) => (
          <FieldControl
            key={field.key}
            field={field}
            value={form[field.key]}
            onChange={(value) => setField(field.key, value)}
            clearChecked={clearSecrets.has(field.key)}
            onClearChange={(checked) =>
              setClearSecrets((prev) => {
                const next = new Set(prev);
                if (checked) {
                  next.add(field.key);
                } else {
                  next.delete(field.key);
                }
                return next;
              })
            }
          />
        ))}
      </div>
      {probe && (
        <div
          className={`mt-4 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${
            probe.ok ? "border-success/40 bg-success/10 text-success" : "border-danger/40 bg-danger/10 text-danger"
          }`}
        >
          {probe.ok ? (
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          ) : (
            <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          )}
          <span>
            {probe.ok
              ? `${t("settings.probeOk")}（${probe.models.length} 个模型）`
              : `${t("settings.probeFailed")}：${probe.error}`}
          </span>
        </div>
      )}
      {status.kind !== "idle" && (
        <div
          className={`mt-3 flex items-center gap-2 text-xs ${
            status.kind === "error" ? "text-danger" : status.kind === "ok" ? "text-success" : "text-muted"
          }`}
        >
          {status.kind === "busy" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {status.kind === "ok" && <CheckCircle2 className="h-3.5 w-3.5" />}
          {status.kind === "error" && <CircleAlert className="h-3.5 w-3.5" />}
          {status.kind === "error"
            ? status.message
            : status.kind === "ok"
              ? status.message
              : t("settings.working")}
        </div>
      )}
      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-border/60 pt-4">
        {area === "models" && (
          <button
            type="button"
            onClick={() => void handleProbe()}
            disabled={probing}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent disabled:opacity-50"
          >
            {probing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
            {t("settings.testConnection")}
          </button>
        )}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => void handleDiscard()}
            className="rounded-lg px-3 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
          >
            {t("common.discard")}
          </button>
          <button
            type="button"
            onClick={() => void handleSaveDraft()}
            disabled={status.kind === "busy"}
            className="rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent disabled:opacity-50"
          >
            {t("common.save")}
          </button>
          <button
            type="button"
            onClick={() => void handleApply()}
            disabled={status.kind === "busy"}
            className="rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
          >
            {t("common.apply")}
          </button>
        </div>
      </div>
    </section>
  );
}

function FieldControl({
  field,
  value,
  onChange,
  clearChecked,
  onClearChange,
}: {
  field: SettingsFieldMeta;
  value: unknown;
  onChange: (value: unknown) => void;
  clearChecked: boolean;
  onClearChange: (checked: boolean) => void;
}) {
  const { t } = useI18n();
  const inputId = useId();
  const label = t(field.label_key);
  const description = field.description_key ? t(field.description_key) : "";
  const inputClass =
    "w-full rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none transition-colors focus:border-primary/60";

  return (
    <div className="block">
      <label
        htmlFor={inputId}
        className="mb-1 flex items-baseline gap-2 text-sm font-medium"
      >
        {label}
        {field.effect === "restart" && (
          <span className="text-[10px] font-normal text-muted">（{t("settings.restartRequired")}）</span>
        )}
      </label>
      {description && <p className="mb-1 text-xs text-muted">{description}</p>}
      {field.type === "choice" ? (
        <select id={inputId} className={inputClass} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
          {field.choices?.map((choice) => (
            <option key={choice} value={choice}>
              {choice === "" ? t("settings.choiceDefault") : choice}
            </option>
          ))}
        </select>
      ) : field.type === "bool" ? (
        <input
          id={inputId}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
          className="h-4 w-4 accent-[var(--primary)]"
        />
      ) : field.type === "float" ? (
        <input
          id={inputId}
          type="number"
          step="0.1"
          min={0}
          max={2}
          className={inputClass}
          value={typeof value === "number" ? value : ""}
          onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        />
      ) : field.type === "int" ? (
        <input
          id={inputId}
          type="number"
          className={inputClass}
          value={typeof value === "number" ? value : ""}
          onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        />
      ) : field.type === "string_list" ? (
        <textarea
          id={inputId}
          className={`${inputClass} font-mono text-xs`}
          rows={2}
          value={Array.isArray(value) ? value.join("\n") : ""}
          onChange={(e) =>
            onChange(
              e.target.value
                .split("\n")
                .map((s) => s.trim())
                .filter(Boolean)
            )
          }
        />
      ) : field.type === "secret" ? (
        <div className="flex items-center gap-2">
          <input
            id={inputId}
            type="password"
            className={inputClass}
            placeholder={typeof value === "string" && value ? value : t("settings.secretPlaceholder")}
            onChange={(e) => onChange(e.target.value)}
            autoComplete="off"
          />
          <label className="flex shrink-0 items-center gap-1 text-xs text-muted">
            <input
              type="checkbox"
              checked={clearChecked}
              onChange={(e) => onClearChange(e.target.checked)}
              className="h-3.5 w-3.5 accent-[var(--primary)]"
            />
            {t("settings.clearSecret")}
          </label>
        </div>
      ) : (
        <input
          id={inputId}
          type="text"
          className={inputClass}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}
