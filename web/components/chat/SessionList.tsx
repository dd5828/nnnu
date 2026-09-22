"use client";

/** 会话列表（§7.21）：新建/切换/重命名（双击标题）/删除。 */

import { useState } from "react";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
import { useChatStore } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";

export default function SessionList() {
  const { t } = useI18n();
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const active = useChatStore((s) => s.active);
  const newSession = useChatStore((s) => s.newSession);
  const selectSession = useChatStore((s) => s.selectSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");

  const sorted = [...sessions].sort((a, b) => b.updated_at - a.updated_at);

  const startRename = (id: string, title: string) => {
    setEditingId(id);
    setEditingTitle(title);
  };

  const commitRename = (id: string) => {
    const title = editingTitle.trim();
    setEditingId(null);
    if (title) {
      void renameSession(id, title);
    }
  };

  const handleDelete = (id: string) => {
    if (window.confirm(t("chat.deleteConfirm"))) {
      void deleteSession(id);
    }
  };

  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-border/70 bg-sidebar-bg/50 md:flex">
      <div className="flex items-center justify-between px-3 pb-1 pt-3">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">
          {t("chat.sessionList")}
        </span>
        <button
          type="button"
          onClick={() => void newSession()}
          className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
          aria-label={t("chat.newSession")}
          title={t("chat.newSession")}
        >
          <Plus className="h-4 w-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
        {sorted.map((session) => (
          <div
            key={session.id}
            onClick={() => {
              if (!active && session.id !== sessionId) {
                void selectSession(session.id);
              }
            }}
            className={`group flex cursor-pointer items-center gap-1 rounded-lg px-2 py-1.5 text-sm transition-colors ${
              session.id === sessionId ? "bg-accent font-medium" : "hover:bg-accent/50"
            }`}
          >
            {editingId === session.id ? (
              <span
                className="flex min-w-0 flex-1 items-center gap-1"
                onClick={(e) => e.stopPropagation()}
              >
                <input
                  value={editingTitle}
                  onChange={(e) => setEditingTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      commitRename(session.id);
                    } else if (e.key === "Escape") {
                      setEditingId(null);
                    }
                  }}
                  className="w-full rounded border border-border bg-surface px-1.5 py-0.5 text-sm outline-none"
                  autoFocus
                />
                <button
                  type="button"
                  onClick={() => commitRename(session.id)}
                  className="text-success"
                >
                  <Check className="h-3.5 w-3.5" />
                </button>
                <button type="button" onClick={() => setEditingId(null)} className="text-muted">
                  <X className="h-3.5 w-3.5" />
                </button>
              </span>
            ) : (
              <>
                <span className="min-w-0 flex-1 truncate">
                  {session.title || t("chat.untitled")}
                </span>
                <span
                  className="hidden shrink-0 items-center gap-0.5 group-hover:flex"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    onClick={() => startRename(session.id, session.title)}
                    className="rounded p-1 text-muted hover:text-foreground"
                    aria-label={t("chat.renameSession")}
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(session.id)}
                    className="rounded p-1 text-muted hover:text-danger"
                    aria-label={t("chat.deleteSession")}
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </span>
              </>
            )}
          </div>
        ))}
        {sorted.length === 0 && (
          <p className="px-2 py-3 text-xs text-muted">{t("chat.noSessions")}</p>
        )}
      </div>
    </aside>
  );
}
