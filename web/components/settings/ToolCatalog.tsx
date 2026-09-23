"use client";

/** 工具目录（§7.2 整体验收"设置页可看到全部工具及 schema"）。
 *
 * 数据源 = /api/v1/plugins 内省（§6.11），只读；开关怎么填看上面的「对话」卡片。
 * 说明与成本提示是双语键、由后端按 ?lang= 渲染（§10.2），所以取数要带当前语言；
 * available=false 表示前置条件没配好（如没配图像模型），此时这工具干不了活。
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

type CatalogEntry = { definition: ToolDefinitionMeta; available: boolean };

export default function ToolCatalog() {
  const { t, lang } = useI18n();
  const [catalog, setCatalog] = useState<{ lang: string; entries: CatalogEntry[] } | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await apiFetch<PluginsResponse>(`/api/v1/plugins?lang=${lang}`);
        if (alive) {
          setCatalog({ lang, entries: data.tools });
          setFailed(false);
        }
      } catch {
        if (alive) {
          setFailed(true);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [lang]);

  // 切语言后旧那份对不上了：先当没数据转圈，别把上一语言的说明继续摆着
  const entries = catalog?.lang === lang ? catalog.entries : null;

  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="mb-3">
        <h2 className="text-base font-semibold">{t("settings.toolsTitle")}</h2>
        <p className="mt-0.5 text-xs text-muted">{t("settings.toolsDesc")}</p>
      </div>
      {failed ? (
        <p className="text-xs text-danger">{t("settings.toolsLoadFailed")}</p>
      ) : entries === null ? (
        <Loader2 className="h-4 w-4 animate-spin text-muted" />
      ) : (
        <ul className="divide-y divide-border/60">
          {entries.map(({ definition: tool, available }) => (
            <li key={tool.name} className="py-2">
              <button
                type="button"
                onClick={() => setOpen((prev) => (prev === tool.name ? null : tool.name))}
                className="flex w-full items-start gap-2 text-left"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-medium">{tool.name}</span>
                    <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted">
                      {t(MOUNT_LABEL_KEY[tool.mount])}
                    </span>
                    {!available && (
                      <span className="rounded border border-danger/40 px-1.5 py-0.5 text-[10px] text-danger">
                        {t("settings.toolUnavailable")}
                      </span>
                    )}
                  </div>
                  {tool.description && (
                    <p className="mt-1 text-xs text-muted">{tool.description}</p>
                  )}
                  {tool.cost_hint && (
                    <p className="mt-0.5 text-[11px] text-muted/70">{tool.cost_hint}</p>
                  )}
                </div>
                <ChevronDown
                  className={`mt-0.5 h-3.5 w-3.5 shrink-0 text-muted transition-transform ${
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
