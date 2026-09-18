"use client";

/** 健康卡片：后端版本/内存/运行时长（验收物：首页显示后端 /health 数据）。 */

import { useHealth } from "@/hooks/useHealth";
import { useI18n } from "@/hooks/useI18n";
import { useMounted } from "@/hooks/useMounted";

export default function HealthCard() {
  const { t } = useI18n();
  const mounted = useMounted();
  const { data, isError, isLoading } = useHealth();

  if (!mounted || isLoading) {
    return <Card title={t("health.backend")} rows={[]} loading />;
  }

  if (isError || !data) {
    return (
      <Card
        title={t("health.backend")}
        rows={[{ label: t("health.status"), value: t("health.offline") }]}
      />
    );
  }

  return (
    <Card
      title={t("health.backend")}
      rows={[
        { label: t("health.status"), value: t("health.online") },
        { label: t("health.version"), value: data.version },
        {
          label: t("health.memory"),
          value: data.memory ? `${data.memory.rss_mb} MB` : "—",
        },
        { label: t("health.uptime"), value: t("health.seconds", { n: String(data.uptime_s) }) },
      ]}
    />
  );
}

function Card({
  title,
  rows,
  loading,
}: {
  title: string;
  rows: { label: string; value: string }[];
  loading?: boolean;
}) {
  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {loading ? (
        <p className="text-sm text-muted">…</p>
      ) : (
        <dl className="space-y-1.5 text-sm">
          {rows.map((row) => (
            <div key={row.label} className="flex justify-between gap-4">
              <dt className="text-muted">{row.label}</dt>
              <dd className="font-mono">{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
