"use client";

/**
 * 子代理「历史导入」标签页（§7.12）：扫本机第三方 CLI 的历史 → 勾选导入 → 只读浏览 → 引用到对话。
 *
 * 四步分开是刻意的，每一跳都有它挡的东西：
 *
 * - **先扫后导**。扫描只读不写，把「扫到些什么、哪些已经导过、哪些来源又变了」摊开给用户看；
 *   直接一键导入的话，用户对"进了多少条东西"没有概念（后端还会按 500 个文件截断）。
 * - **导入幂等**。同一份历史再导一次不会多出条目，来源变了则整条覆盖——所以界面上
 *   「已导入/有更新」两个标记必须分开显示，用户才知道这次会不会动到已有数据。
 * - **浏览是只读的**。转录按页取，一次 50 条，翻页按钮改的是 `limit`（从头多取一截），
 *   不拼本地数组——省掉"合并两页"的那套状态机。
 * - **引用走 pending ref 通道**：点「引用到对话」只是往输入区挂一枚 chip，发送时才随消息走；
 *   真正注入模型的是发送那一刻服务端解析出的转录快照（分页读全量），跟这里看到的页无关。
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  Bot,
  ChevronDown,
  ChevronUp,
  Clock,
  FileSearch,
  FolderOpen,
  Import,
  Loader2,
  MessageSquare,
  Quote,
  Search,
  Trash2,
} from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import { useI18n } from "@/hooks/useI18n";
import {
  useAgentImport,
  useAgentImports,
  useDeleteAgentImport,
  useImportAgentSessions,
  useScanAgentImports,
} from "@/hooks/useAgents";
import { useChatStore } from "@/hooks/useChat";
import { errorText } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { AgentImportSession } from "@/types/api";
import type { Language } from "@/i18n";

/** 「最近几天」的可选档位（0 = 不限）。 */
const DAYS = [0, 1, 3, 7, 30];

const PAGE = 50;

export default function AgentImportsPanel({ focusId }: { focusId?: string | null }) {
  const { t, lang } = useI18n();
  const router = useRouter();
  const [wizardOpen, setWizardOpen] = useState(false);
  const [query, setQuery] = useState("");
  // 展开哪一条：`null` = 还没本地表过态（听深链的），`""` = 本地明确关上了。
  // 用「派生」而不是 effect 同步：effect 里 setState 会多渲染一轮，而且深链改一次
  // 就要跟一次——派生的话 `?id=` 变了自然跟着变（消息上的引用 chip 跳过来就是这条路）。
  const [choice, setChoice] = useState<string | null>(null);
  const openId = choice === null ? focusId ?? null : choice || null;

  const list = useAgentImports(query);
  const rows = list.data?.sessions ?? [];

  const openRow = (id: string) => {
    const next = openId === id ? "" : id;
    setChoice(next);
    // 关掉展开时顺手把深链上的 id 摘掉，免得刷新又弹回来
    router.replace(
      next ? `/agents?tab=imports&id=${encodeURIComponent(next)}` : "/agents?tab=imports"
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <p className="text-xs text-muted">{t("agents.importsDesc")}</p>
        <button
          type="button"
          data-testid="ag-scan-toggle"
          onClick={() => setWizardOpen((value) => !value)}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm transition-colors hover:bg-accent"
        >
          <FileSearch className="h-3.5 w-3.5" />
          {wizardOpen ? t("agents.scanClose") : t("agents.scanOpen")}
        </button>
      </div>

      {wizardOpen && <ImportWizard onImported={(ids) => setChoice(ids[0] ?? "")} />}

      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
        <input
          data-testid="ag-import-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("agents.importSearchPlaceholder")}
          className="w-full rounded-lg border border-border bg-surface py-1.5 pl-8 pr-3 text-sm outline-none focus:border-primary"
        />
      </div>

      {list.data && (
        <p data-testid="ag-import-count" className="text-[11px] text-muted">
          {t("agents.importCount", { n: String(list.data.total) })}
        </p>
      )}

      {list.isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : list.error ? (
        <p className="text-sm text-danger">{errorText(list.error, t("common.requestFailed"))}</p>
      ) : rows.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-12 text-center">
          <Bot className="h-6 w-6 text-muted" />
          <p className="max-w-md text-sm text-muted">
            {query ? t("agents.importSearchEmpty", { query }) : t("agents.importsEmpty")}
          </p>
        </div>
      ) : (
        <ul className="grid gap-2">
          {rows.map((session) => (
            <ImportedRow
              key={session.id}
              session={session}
              lang={lang}
              open={openId === session.id}
              onToggle={() => openRow(session.id)}
              // 删掉的正是当前展开的那条：顺手收起（`openRow` 会把深链上的 `?id=` 也摘掉）。
              // 不然地址栏还留着一个已经不存在的 id，刷新就是一次白跑的 404。
              onDeleted={() => {
                if (openId === session.id) {
                  openRow(session.id);
                }
              }}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

/** 导入向导：选天数 → 扫 → 勾选 → 导。状态都在这一处，扫出来的候选只活在这儿。 */
function ImportWizard({ onImported }: { onImported: (ids: string[]) => void }) {
  const { t, lang } = useI18n();
  const scan = useScanAgentImports();
  const importSessions = useImportAgentSessions();
  const [days, setDays] = useState(3);
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const candidates = scan.data?.sessions ?? [];
  const toggle = (id: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const runImport = (ids: string[]) => {
    importSessions.mutate(
      { days, ids },
      {
        onSuccess: (result) => {
          setPicked(new Set());
          // 导完重扫一遍：候选上的「已导入」标记与服务端 digest 对齐（导入幂等，再扫不吃亏）
          scan.mutate({ days });
          onImported(result.imported.map((item) => item.id));
        },
      }
    );
  };

  return (
    <div className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">{t("agents.importDays")}</span>
        {DAYS.map((value) => (
          <button
            key={value}
            type="button"
            data-testid={`ag-days-${value}`}
            data-active={days === value}
            onClick={() => setDays(value)}
            className={`rounded-full px-2.5 py-0.5 text-[11px] transition-colors ${
              days === value
                ? "bg-primary text-primary-foreground"
                : "bg-accent/50 text-muted hover:bg-accent"
            }`}
          >
            {value === 0 ? t("agents.daysAll") : t("agents.daysN", { n: String(value) })}
          </button>
        ))}
        <button
          type="button"
          data-testid="ag-scan"
          disabled={scan.isPending}
          onClick={() => {
            setPicked(new Set());
            scan.mutate({ days });
          }}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {scan.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <FileSearch className="h-3.5 w-3.5" />
          )}
          {scan.isPending ? t("agents.scanning") : t("agents.scan")}
        </button>
      </div>

      {scan.isError && (
        <p data-testid="ag-scan-error" className="text-xs text-danger">
          {errorText(scan.error, t("agents.scanFailed"))}
        </p>
      )}

      {scan.data && (
        <>
          <p data-testid="ag-scan-root" className="truncate font-mono text-[10px] text-muted">
            {t("agents.scanRoot", { root: scan.data.root })}
          </p>
          {scan.data.truncated && (
            <p data-testid="ag-scan-truncated" className="text-[11px] text-danger">
              {t("agents.scanTruncated")}
            </p>
          )}
          {scan.data.skipped.length > 0 && (
            <p data-testid="ag-scan-skipped" className="text-[11px] text-muted">
              {t("agents.scanSkipped", { n: String(scan.data.skipped.length) })} ·{" "}
              {scan.data.skipped
                .slice(0, 3)
                .map((item) => `${item.path}: ${item.reason}`)
                .join("; ")}
            </p>
          )}

          {candidates.length === 0 ? (
            <p data-testid="ag-scan-empty" className="text-xs text-muted">
              {t("agents.scanEmpty")}
            </p>
          ) : (
            <>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  data-testid="ag-pick-all"
                  onClick={() =>
                    setPicked(
                      picked.size === candidates.length
                        ? new Set()
                        : new Set(candidates.map((item) => item.id))
                    )
                  }
                  className="text-[11px] text-muted transition-colors hover:text-foreground"
                >
                  {picked.size === candidates.length ? t("agents.pickNone") : t("agents.pickAll")}
                </button>
                <span className="text-[11px] text-muted">
                  {t("agents.pickedCount", {
                    picked: String(picked.size),
                    total: String(candidates.length),
                  })}
                </span>
                <button
                  type="button"
                  data-testid="ag-import-picked"
                  disabled={picked.size === 0 || importSessions.isPending}
                  onClick={() => runImport([...picked])}
                  className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  {importSessions.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Import className="h-3.5 w-3.5" />
                  )}
                  {importSessions.isPending ? t("agents.importing") : t("agents.importPicked")}
                </button>
              </div>

              <ul className="max-h-80 space-y-1 overflow-y-auto">
                {candidates.map((item) => (
                  <li
                    key={item.id}
                    data-testid={`ag-candidate-${item.id}`}
                    data-imported={Boolean(item.imported)}
                    className="flex items-start gap-2 rounded-lg border border-border px-2.5 py-2"
                  >
                    <input
                      type="checkbox"
                      data-testid={`ag-candidate-pick-${item.id}`}
                      checked={picked.has(item.id)}
                      onChange={() => toggle(item.id)}
                      className="mt-1 h-3.5 w-3.5 shrink-0 accent-[var(--primary)]"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="truncate text-xs font-medium">
                          {item.title || t("agents.untitled")}
                        </span>
                        {item.imported && (
                          <span
                            data-testid={`ag-candidate-imported-${item.id}`}
                            className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted"
                          >
                            {item.changed ? t("agents.changed") : t("agents.imported")}
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 truncate text-[10px] text-muted">
                        {[
                          item.project,
                          formatDateTime(item.ended_at ?? item.started_at ?? 0, lang),
                          t("agents.messages", { n: String(item.message_count) }),
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}

      {importSessions.isError && (
        <p data-testid="ag-import-error" className="text-xs text-danger">
          {errorText(importSessions.error, t("agents.importFailed"))}
        </p>
      )}

      {importSessions.data && (
        <p data-testid="ag-import-result" className="text-[11px] text-muted">
          {t("agents.importResult", {
            imported: String(importSessions.data.imported.length),
            unchanged: String(importSessions.data.unchanged.length),
          })}
          {importSessions.data.errors.length > 0
            ? ` · ${t("agents.importMissed", { n: String(importSessions.data.errors.length) })}`
            : ""}
        </p>
      )}
    </div>
  );
}

function ImportedRow({
  session,
  lang,
  open,
  onToggle,
  onDeleted,
}: {
  session: AgentImportSession;
  lang: Language;
  open: boolean;
  onToggle: () => void;
  onDeleted: () => void;
}) {
  const { t } = useI18n();
  const remove = useDeleteAgentImport();
  const addPendingRef = useChatStore((s) => s.addPendingRef);
  const pendingRefs = useChatStore((s) => s.pendingRefs);
  const title = session.title || t("agents.untitled");
  const referenced = pendingRefs.some(
    (ref) => ref.kind === "agent_session" && ref.agent_session_id === session.id
  );

  const startDelete = async () => {
    const ok = await confirmDialog({
      message: t("agents.importDeleteConfirm", { name: title }),
      danger: true,
      confirmLabel: t("agents.delete"),
    });
    if (ok) {
      remove.mutate(session.id, { onSuccess: onDeleted });
    }
  };

  return (
    <li
      data-testid={`ag-import-${session.id}`}
      className="rounded-xl border border-border bg-surface"
    >
      <div className="flex flex-wrap items-center gap-2 p-3">
        <button
          type="button"
          data-testid={`ag-import-open-${session.id}`}
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          {open ? (
            <ChevronUp className="h-3.5 w-3.5 shrink-0 text-muted" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
          )}
          <Bot className="h-3.5 w-3.5 shrink-0 text-muted" />
          <span className="truncate text-sm">{title}</span>
          <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted">
            {session.source === "claude_code" ? t("agents.kindClaudeCode") : session.source}
          </span>
        </button>
        <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-muted">
          <MessageSquare className="h-3 w-3" />
          {session.message_count}
        </span>
        <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-muted">
          <Clock className="h-3 w-3" />
          {formatDateTime(session.ended_at ?? session.started_at ?? 0, lang)}
        </span>
        <button
          type="button"
          data-testid={`ag-import-ref-${session.id}`}
          data-referenced={referenced}
          onClick={() =>
            addPendingRef({ kind: "agent_session", agent_session_id: session.id, label: title })
          }
          title={t("agents.refToChat")}
          className={`inline-flex shrink-0 items-center gap-1 rounded-lg border px-2 py-1 text-[11px] transition-colors ${
            referenced
              ? "border-primary/40 bg-primary/10 text-primary"
              : "border-border text-muted hover:bg-accent hover:text-foreground"
          }`}
        >
          <Quote className="h-3 w-3" />
          {t("agents.refToChat")}
        </button>
        <button
          type="button"
          data-testid={`ag-import-delete-${session.id}`}
          disabled={remove.isPending}
          onClick={() => void startDelete()}
          title={t("agents.delete")}
          className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger disabled:opacity-50"
        >
          {remove.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Trash2 className="h-3.5 w-3.5" />
          )}
        </button>
      </div>

      {session.cwd && (
        <p className="truncate px-3 pb-2 font-mono text-[10px] text-muted" title={session.cwd}>
          <FolderOpen className="mr-1 inline h-3 w-3" />
          {session.cwd}
        </p>
      )}

      {open && <Transcript sessionId={session.id} />}
    </li>
  );
}

/** 只读转录：一次取 `limit` 条，翻页靠加大 limit（后端按页切，前端不拼数组）。 */
function Transcript({ sessionId }: { sessionId: string }) {
  const { t, lang } = useI18n();
  const [limit, setLimit] = useState(PAGE);
  const detail = useAgentImport(sessionId, 0, limit);
  const messages = detail.data?.messages ?? [];

  return (
    <div className="space-y-2 border-t border-border p-3">
      {detail.isLoading ? (
        <div className="flex justify-center py-4">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : detail.error ? (
        <p className="text-xs text-danger">{errorText(detail.error, t("common.requestFailed"))}</p>
      ) : messages.length === 0 ? (
        <p data-testid={`ag-transcript-empty-${sessionId}`} className="text-xs text-muted">
          {t("agents.transcriptEmpty")}
        </p>
      ) : (
        <>
          <ul data-testid={`ag-transcript-${sessionId}`} className="space-y-2">
            {messages.map((message, index) => (
              <li key={`${index}-${message.ts ?? 0}`} className="rounded-lg bg-accent/30 p-2">
                <p className="flex items-center gap-2 text-[10px] text-muted">
                  <span
                    data-role={message.role}
                    className={message.role === "user" ? "text-primary" : "text-muted"}
                  >
                    {message.role === "user" ? t("agents.roleUser") : t("agents.roleAssistant")}
                  </span>
                  {message.ts ? <span>{formatDateTime(message.ts, lang)}</span> : null}
                </p>
                <p className="mt-1 whitespace-pre-wrap break-words text-xs">{message.text}</p>
              </li>
            ))}
          </ul>
          {detail.data?.has_more && (
            <button
              type="button"
              data-testid={`ag-transcript-more-${sessionId}`}
              disabled={detail.isFetching}
              onClick={() => setLimit((value) => value + PAGE)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition-colors hover:bg-accent disabled:opacity-50"
            >
              {detail.isFetching && <Loader2 className="h-3 w-3 animate-spin" />}
              {t("agents.loadMore", {
                rest: String((detail.data?.total ?? 0) - messages.length),
              })}
            </button>
          )}
        </>
      )}
    </div>
  );
}
