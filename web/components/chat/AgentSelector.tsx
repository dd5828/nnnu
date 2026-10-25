"use client";

/** 子代理选择器（§7.12）：本回合要咨询哪台编码 CLI，会话级粘性。
 *
 * 与模型选择器三处不同，都是后端定的规矩：
 *
 * - **一条连接都没有就整个不显示**。`consult_subagent` 是靠「有连接」这个门挂上去的
 *   （`_agent_gate`），没连接时摆个空下拉只是占地方；建连接在 `/agents` 页。
 * - **首行是「自动」而不是「跟随默认」**：后端 `resolve` 的语义是「请求显式指定 >
 *   会话粘性选中 > 只有一条就用它」，所以 `null` 不是"没选"，是"交给后端挑"。
 * - **没接入链路的类型不给选**（灰掉 + 一句原因）：本批只打通了 Claude Code，
 *   选了一条 codex 再咨询只会每次都失败——不如在选的时候就拦住。
 */

import { useEffect, useRef, useState } from "react";
import { Check, Terminal } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { useAgentConnections } from "@/hooks/useAgents";

const KIND_KEY: Record<string, string> = {
  claude_code: "kindClaudeCode",
  codex: "kindCodex",
  gemini: "kindGemini",
  kimi: "kindKimi",
  opencode: "kindOpencode",
  mimo: "kindMimo",
};

export default function AgentSelector() {
  const { t } = useI18n();
  const agentConnection = useChatStore((s) => s.agentConnection);
  const setAgentConnection = useChatStore((s) => s.setAgentConnection);
  const { data } = useAgentConnections();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const connections = data?.connections ?? [];
  // 悬空值（连接在别处被删了）照实显示 id：静默回落成「自动」会让用户以为选还在
  const current = connections.find((item) => item.id === agentConnection);
  const label = agentConnection === null ? t("chat.agentAuto") : current?.label ?? agentConnection;

  useEffect(() => {
    if (!open) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  if (connections.length === 0) {
    return null;
  }

  const pick = (value: string | null) => {
    setAgentConnection(value);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        data-testid="agent-selector"
        onClick={() => setOpen((prev) => !prev)}
        aria-label={t("chat.selectAgent")}
        aria-expanded={open}
        title={t("chat.agentHint")}
        className={`flex max-w-[10rem] items-center gap-1.5 rounded-lg p-2 text-xs transition-colors ${
          agentConnection !== null
            ? "text-primary hover:bg-primary/10"
            : "text-muted hover:bg-accent hover:text-foreground"
        }`}
      >
        <Terminal className="h-4 w-4 shrink-0" />
        <span className="truncate">{label}</span>
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1.5 w-[min(320px,calc(100vw-32px))] overflow-hidden rounded-xl border border-border bg-surface shadow-lg">
          <div className="max-h-[300px] overflow-y-auto py-1">
            <button
              type="button"
              data-testid="agent-option-auto"
              onClick={() => pick(null)}
              className={`flex w-full flex-col items-start gap-0.5 px-3 py-1.5 text-left text-xs transition-colors ${
                agentConnection === null ? "bg-primary/5" : "hover:bg-accent"
              }`}
            >
              <span className="flex w-full items-center gap-2.5">
                <span className="min-w-0 flex-1 truncate font-medium">{t("chat.agentAuto")}</span>
                {agentConnection === null && (
                  <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                )}
              </span>
              <span className="w-full text-[11px] text-muted">{t("chat.agentAutoHint")}</span>
            </button>

            {connections.map((connection) => {
              const selected = agentConnection === connection.id;
              const kindLabel = KIND_KEY[connection.kind]
                ? t(`agents.${KIND_KEY[connection.kind]}`)
                : connection.kind;
              return (
                <button
                  key={connection.id}
                  type="button"
                  data-testid="agent-option"
                  data-value={connection.id}
                  disabled={!connection.supported}
                  title={connection.supported ? connection.cwd : t("chat.agentNotWired")}
                  onClick={() => pick(connection.id)}
                  className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-xs transition-colors ${
                    !connection.supported
                      ? "cursor-not-allowed opacity-50"
                      : selected
                        ? "bg-primary/5"
                        : "hover:bg-accent"
                  }`}
                >
                  <Terminal
                    className={`h-3.5 w-3.5 shrink-0 ${selected ? "text-primary" : "text-muted"}`}
                  />
                  <span className="min-w-0 flex-1 truncate font-medium">{connection.label}</span>
                  {!connection.supported && (
                    <span className="shrink-0 rounded bg-accent px-1 py-0.5 text-[10px] text-muted">
                      {t("agents.notWired")}
                    </span>
                  )}
                  <span className="shrink-0 text-[10px] text-muted">{kindLabel}</span>
                  {selected && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
