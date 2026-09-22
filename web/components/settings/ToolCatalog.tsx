"use client";

/** 工具目录（§7.2 整体验收"设置页可看到全部工具及 schema"）。
 *
 * 数据源 = /api/v1/plugins 内省（§6.11），只读；开关怎么填看上面的「对话」卡片。
 */

import { useEffect, useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useI18n } from "@/hooks/useI18n";
import type { PluginsResponse, ToolDefinitionMeta } from "@/types/api";

const MOUNT_LABEL_KEY: Record<ToolDefinitionMeta["mount"], string> = {
  always: "settings.mountAlways",
  context_gated: "settings.mountContext",
  user_toggleable: "settings.mountToggleable",
};

export default function ToolCatalog() {
  const { t } = useI18n();
  const [tools, setTools] = useState<ToolDefinitionMeta[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<PluginsResponse>("/api/v1/plugins")
      .then((data) => setTools(data.tools.map((item) => item.definition)))
      .catch(() => setFailed(true));
  }, []);

  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="mb-3">
        <h2 className="text-base font-semibold">{t("settings.toolsTitle")}</h2>
        <p className="mt-0.5 text-xs text-muted">{t("settings.toolsDesc")}</p>
      </div>
      {failed ? (
        <p className="text-xs text-danger">{t("settings.toolsLoadFailed")}</p>
      ) : tools === null ? (
        <Loader2 className="h-4 w-4 animate-spin text-muted" />
      ) : (
        <ul className="divide-y divide-border/60">
          {tools.map((tool) => (
            <li key={tool.name} className="py-2">
              <button
                type="button"
                onClick={() => setOpen((prev) => (prev === tool.name ? null : tool.name))}
                className="flex w-full items-center gap-2 text-left"
              >
                <span className="font-mono text-xs font-medium">{tool.name}</span>
                <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted">
                  {t(MOUNT_LABEL_KEY[tool.mount])}
                </span>
                {tool.cost_hint && (
                  <span className="min-w-0 flex-1 truncate text-xs text-muted">
                    {tool.cost_hint}
                  </span>
                )}
                <ChevronDown
                  className={`ml-auto h-3.5 w-3.5 shrink-0 text-muted transition-transform ${
                    open === tool.name ? "" : "-rotate-90"
                  }`}
                />
              </button>
              {open === tool.name && (
                <pre className="mt-1.5 max-h-56 overflow-auto rounded-lg bg-background/40 p-2 font-mono text-[11px] whitespace-pre-wrap break-all">
                  {JSON.stringify(tool.parameters, null, 2)}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
