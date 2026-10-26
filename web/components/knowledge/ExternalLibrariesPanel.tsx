"use client";

/**
 * 外部库（§7.9 P14）：Obsidian / 腾讯 IMA / MarginNote 4。
 *
 * 三块各自独立：
 * - Obsidian：挂载本地 vault，列笔记、读全文、把修改写回原文件（只写 .md）。
 * - IMA：看配置状态、测连通、浏览文件夹、检索并读正文。
 * - MarginNote：配对设备（令牌只显示一次）、看已同步对象、检索。
 *
 * 这里只做「看与少量写」，大量写回仍然交给聊天里的工具；外部库面板的职责是
 * 让用户确认连接是通的、数据在不在。
 */

import { useState } from "react";
import Link from "next/link";
import {
  BookOpen,
  Check,
  ChevronLeft,
  Clipboard,
  FileText,
  Folder,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Smartphone,
  Trash2,
  X,
} from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import {
  useAddVault,
  useImaBrowse,
  useImaItem,
  useImaSearch,
  useImaTest,
  useLibrariesOverview,
  useMnDevices,
  useMnObjects,
  useMnPair,
  useMnRevoke,
  useObsidianNote,
  useObsidianNotes,
  useObsidianSearch,
  useRemoveVault,
  useWriteObsidianNote,
} from "@/hooks/useExternalLibraries";
import { useI18n } from "@/hooks/useI18n";
import { errorText } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { MarginNoteObject, ObsidianNote, ObsidianSearchHit } from "@/types/api";

type Tab = "obsidian" | "ima" | "marginnote";

const MN_TYPES = ["note", "excerpt", "card", "mindmap_node", "document", "comment"];

export default function ExternalLibrariesPanel() {
  const { t } = useI18n();
  const { data, isLoading, error } = useLibrariesOverview();
  const [tab, setTab] = useState<Tab>("obsidian");
  const vaults = data?.obsidian.vaults ?? [];
  const ima = data?.ima;
  const mn = data?.marginnote;

  const tabs: { id: Tab; label: string; badge?: number }[] = [
    { id: "obsidian", label: t("knowledge.externalTabObsidian"), badge: vaults.length },
    { id: "ima", label: t("knowledge.externalTabIma") },
    { id: "marginnote", label: t("knowledge.externalTabMarginnote"), badge: mn?.objects },
  ];

  return (
    <section
      data-testid="external-libraries"
      className="rounded-xl border border-border bg-surface p-5"
    >
      <div className="mb-3">
        <h2 className="text-base font-semibold">{t("knowledge.externalTitle")}</h2>
        <p className="mt-0.5 text-xs text-muted">{t("knowledge.externalDesc")}</p>
      </div>

      <div className="mb-4 flex flex-wrap gap-1.5" role="tablist">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            onClick={() => setTab(item.id)}
            className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors ${
              tab === item.id
                ? "bg-primary/10 text-primary"
                : "text-muted hover:bg-accent hover:text-foreground"
            }`}
          >
            {item.label}
            {item.badge !== undefined && item.badge > 0 && (
              <span className="rounded-full bg-accent px-1.5 text-[10px] text-muted">
                {item.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="flex justify-center py-6">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>
      ) : tab === "obsidian" ? (
        <ObsidianPanel vaults={vaults} />
      ) : tab === "ima" ? (
        <ImaPanel configured={Boolean(ima?.configured)} statusError={ima?.error ?? ""} />
      ) : (
        <MarginNotePanel />
      )}
    </section>
  );
}

// ================= Obsidian =================

function ObsidianPanel({ vaults }: { vaults: { vault_id: string; name: string; path: string }[] }) {
  const { t } = useI18n();
  const add = useAddVault();
  const remove = useRemoveVault();
  const [formOpen, setFormOpen] = useState(false);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [vaultId, setVaultId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  const activeVault = vaults.find((vault) => vault.vault_id === vaultId) ?? vaults[0] ?? null;
  const notes = useObsidianNotes(activeVault?.vault_id ?? null);
  const search = useObsidianSearch(activeVault?.vault_id ?? null, query);

  const submit = async () => {
    if (!path.trim()) {
      setFormError(t("knowledge.obsidianPathRequired"));
      return;
    }
    setFormError(null);
    try {
      const result = await add.mutateAsync({ path: path.trim(), name: name.trim() });
      setVaultId(result.vault.vault_id);
      setPath("");
      setName("");
      setFormOpen(false);
    } catch (addError) {
      setFormError(errorText(addError, t("common.requestFailed")));
    }
  };

  const removeVault = (id: string, label: string) => {
    void confirmDialog({
      message: t("knowledge.obsidianRemoveConfirm", { name: label }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        remove.mutate(id);
        if (vaultId === id) {
          setVaultId(null);
          setSelectedPath(null);
        }
      }
    });
  };

  const hits: ObsidianSearchHit[] = search.data?.hits ?? [];
  const noteRows = notes.data?.notes ?? [];

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <BookOpen className="h-4 w-4 text-muted" />
        <span className="text-sm font-medium">{t("knowledge.obsidianVaults")}</span>
        <button
          type="button"
          data-testid="obsidian-add"
          onClick={() => setFormOpen((open) => !open)}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs transition-colors hover:bg-accent"
        >
          {formOpen ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          {formOpen ? t("knowledge.cancel") : t("knowledge.obsidianAdd")}
        </button>
      </div>

      {formOpen && (
        <div className="space-y-2 rounded-lg bg-accent/40 p-3">
          <input
            data-testid="obsidian-vault-path"
            value={path}
            onChange={(event) => {
              setPath(event.target.value);
              setFormError(null);
            }}
            placeholder={t("knowledge.obsidianPathPlaceholder")}
            className="w-full rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/60"
          />
          <div className="flex gap-2">
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t("knowledge.obsidianNamePlaceholder")}
              className="min-w-0 flex-1 rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/60"
            />
            <button
              type="button"
              data-testid="obsidian-vault-add"
              disabled={add.isPending}
              onClick={() => void submit()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
            >
              {add.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {t("knowledge.obsidianMount")}
            </button>
          </div>
          {formError && <p className="text-xs text-danger">{formError}</p>}
        </div>
      )}

      {vaults.length === 0 ? (
        <p className="text-sm text-muted">{t("knowledge.obsidianEmpty")}</p>
      ) : (
        <>
          <ul className="space-y-1.5">
            {vaults.map((vault) => (
              <li
                key={vault.vault_id}
                className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                  activeVault?.vault_id === vault.vault_id
                    ? "border-primary/50 bg-primary/5"
                    : "border-border/70"
                }`}
              >
                <button
                  type="button"
                  data-testid="obsidian-vault"
                  data-name={vault.name}
                  onClick={() => {
                    setVaultId(vault.vault_id);
                    setSelectedPath(null);
                    setQuery("");
                  }}
                  className="min-w-0 flex-1 text-left"
                >
                  <span className="truncate font-medium">{vault.name}</span>
                  <span className="ml-2 truncate text-xs text-muted">{vault.path}</span>
                </button>
                <button
                  type="button"
                  title={t("knowledge.obsidianRemove")}
                  onClick={() => removeVault(vault.vault_id, vault.name)}
                  disabled={remove.isPending}
                  className="rounded-lg p-1 text-muted transition-colors hover:bg-accent hover:text-danger disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>

          {activeVault && !selectedPath && (
            <div className="rounded-lg border border-border/70 p-3">
              <div className="mb-2 flex items-center gap-2">
                <Search className="h-3.5 w-3.5 text-muted" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t("knowledge.obsidianSearchPlaceholder")}
                  className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted"
                />
                {query.trim() && (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    className="rounded p-0.5 text-muted hover:bg-accent"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              {notes.isLoading ? (
                <div className="flex justify-center py-3">
                  <Loader2 className="h-4 w-4 animate-spin text-muted" />
                </div>
              ) : query.trim() ? (
                hits.length === 0 ? (
                  <p className="text-xs text-muted">{t("knowledge.obsidianNoHits")}</p>
                ) : (
                  <ul className="space-y-1">
                    {hits.map((hit) => (
                      <li key={hit.path}>
                        <button
                          type="button"
                          onClick={() => setSelectedPath(hit.path)}
                          className="w-full rounded px-2 py-1.5 text-left transition-colors hover:bg-accent"
                        >
                          <span className="flex items-center gap-1.5 text-sm">
                            <FileText className="h-3.5 w-3.5 shrink-0 text-muted" />
                            {hit.path}
                          </span>
                          <span className="mt-0.5 block truncate text-xs text-muted">
                            {hit.snippet}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )
              ) : noteRows.length === 0 ? (
                <p className="text-xs text-muted">{t("knowledge.obsidianNoNotes")}</p>
              ) : (
                <ul className="space-y-0.5">
                  {noteRows.map((note) => (
                    <li key={note.path}>
                      <button
                        type="button"
                        data-testid="obsidian-note"
                        data-path={note.path}
                        onClick={() => setSelectedPath(note.path)}
                        className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent"
                      >
                        <FileText className="h-3.5 w-3.5 shrink-0 text-muted" />
                        <span className="truncate">{note.path}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {activeVault && selectedPath && (
            <ObsidianNoteEditor
              vaultId={activeVault.vault_id}
              path={selectedPath}
              onClose={() => setSelectedPath(null)}
            />
          )}
        </>
      )}
    </div>
  );
}

function ObsidianNoteEditor({
  vaultId,
  path,
  onClose,
}: {
  vaultId: string;
  path: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const { data, isLoading, error } = useObsidianNote(vaultId, path);
  if (isLoading) {
    return (
      <div className="flex justify-center py-4">
        <Loader2 className="h-4 w-4 animate-spin text-muted" />
      </div>
    );
  }
  if (error || !data) {
    return <p className="text-sm text-danger">{errorText(error, t("common.requestFailed"))}</p>;
  }
  return <ObsidianEditorBody key={data.path} vaultId={vaultId} note={data} onClose={onClose} />;
}

function ObsidianEditorBody({
  vaultId,
  note,
  onClose,
}: {
  vaultId: string;
  note: ObsidianNote;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const write = useWriteObsidianNote(vaultId);
  const [content, setContent] = useState(note.content);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (mode: "overwrite" | "append") => {
    setError(null);
    setSaved(false);
    try {
      await write.mutateAsync({ path: note.path, content, mode });
      setSaved(true);
    } catch (writeError) {
      setError(errorText(writeError, t("common.requestFailed")));
    }
  };

  return (
    <div className="rounded-lg border border-primary/40 p-3">
      <div className="mb-2 flex items-center gap-2">
        <FileText className="h-4 w-4 text-primary" />
        <span className="truncate text-sm font-medium">{note.path}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded p-1 text-muted hover:bg-accent"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <textarea
        data-testid="obsidian-editor"
        value={content}
        onChange={(event) => {
          setContent(event.target.value);
          setSaved(false);
        }}
        rows={12}
        className="w-full resize-y rounded-lg border border-border bg-background/40 px-3 py-2 font-mono text-xs outline-none focus:border-primary/60"
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="obsidian-save"
          disabled={write.isPending}
          onClick={() => void save("overwrite")}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
        >
          {write.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Check className="h-3 w-3" />
          )}
          {t("knowledge.obsidianSave")}
        </button>
        <button
          type="button"
          disabled={write.isPending || !content.trim()}
          onClick={() => void save("append")}
          className="rounded-lg border border-border px-3 py-1.5 text-xs transition-colors hover:bg-accent disabled:opacity-50"
        >
          {t("knowledge.obsidianAppend")}
        </button>
        {saved && <span className="text-xs text-success">{t("knowledge.obsidianSaved")}</span>}
        {error && <span className="text-xs text-danger">{error}</span>}
      </div>
    </div>
  );
}

// ================= IMA =================

function ImaPanel({ configured, statusError }: { configured: boolean; statusError: string }) {
  const { t } = useI18n();
  const test = useImaTest();
  const [folderId, setFolderId] = useState("");
  const [query, setQuery] = useState("");
  const [mediaId, setMediaId] = useState<string | null>(null);
  const browse = useImaBrowse(configured, folderId);
  const search = useImaSearch(query);
  const item = useImaItem(mediaId);

  if (!configured) {
    return (
      <div className="rounded-lg border border-dashed border-border p-4">
        <p className="text-sm text-muted">{t("knowledge.imaNotConfigured")}</p>
        {statusError && <p className="mt-1 text-xs text-danger">{statusError}</p>}
        <Link href="/settings" className="mt-2 inline-block text-xs text-primary underline">
          {t("knowledge.imaSettingsHint")}
        </Link>
      </div>
    );
  }

  const page = search.data ?? browse.data;
  const documents = page?.documents ?? [];
  const folders = page?.folders ?? [];
  const searching = query.trim().length > 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm">
          <span className="h-2 w-2 rounded-full bg-success" />
          {t("knowledge.imaConfigured")}
        </span>
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1 text-xs transition-colors hover:bg-accent disabled:opacity-50"
        >
          {test.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          {t("knowledge.imaTest")}
        </button>
        {test.data?.ok && (
          <span className="text-xs text-success">
            {t("knowledge.imaTestOk", { name: test.data.name })}
          </span>
        )}
        {test.data && !test.data.ok && (
          <span className="text-xs text-danger">{test.data.error}</span>
        )}
        {test.error && (
          <span className="text-xs text-danger">
            {errorText(test.error, t("common.requestFailed"))}
          </span>
        )}
      </div>

      <div className="rounded-lg border border-border/70 p-3">
        <div className="mb-2 flex items-center gap-2">
          <Search className="h-3.5 w-3.5 text-muted" />
          <input
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setFolderId("");
            }}
            placeholder={t("knowledge.imaSearchPlaceholder")}
            className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted"
          />
          {query.trim() && (
            <button
              type="button"
              onClick={() => setQuery("")}
              className="rounded p-0.5 text-muted hover:bg-accent"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        {!searching && folderId && (
          <button
            type="button"
            onClick={() => setFolderId("")}
            className="mb-2 inline-flex items-center gap-1 text-xs text-primary"
          >
            <ChevronLeft className="h-3 w-3" />
            {t("knowledge.imaBackRoot")}
          </button>
        )}
        {!searching && page?.path && page.path.length > 0 && (
          <p className="mb-1 text-xs text-muted">{page.path.join(" / ")}</p>
        )}

        {browse.isLoading || search.isLoading ? (
          <div className="flex justify-center py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : browse.error || search.error ? (
          <p className="text-xs text-danger">
            {errorText(browse.error ?? search.error, t("common.requestFailed"))}
          </p>
        ) : folders.length === 0 && documents.length === 0 ? (
          <p className="text-xs text-muted">{t("knowledge.imaNoResults")}</p>
        ) : (
          <ul className="space-y-0.5">
            {folders.map((folder) => (
              <li key={folder.folder_id}>
                <button
                  type="button"
                  onClick={() => setFolderId(folder.folder_id)}
                  className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent"
                >
                  <Folder className="h-3.5 w-3.5 shrink-0 text-muted" />
                  <span className="truncate">{folder.name}</span>
                  <span className="ml-auto text-xs text-muted">
                    {t("knowledge.imaFolderCount", {
                      docs: String(folder.file_number),
                      folders: String(folder.folder_number),
                    })}
                  </span>
                </button>
              </li>
            ))}
            {documents.map((document) => (
              <li key={document.media_id}>
                <button
                  type="button"
                  onClick={() => setMediaId(document.media_id)}
                  className="w-full rounded px-2 py-1.5 text-left transition-colors hover:bg-accent"
                >
                  <span className="flex items-center gap-1.5 text-sm">
                    <FileText className="h-3.5 w-3.5 shrink-0 text-muted" />
                    <span className="truncate">{document.title}</span>
                  </span>
                  {document.highlight && (
                    <span className="mt-0.5 block truncate text-xs text-muted">
                      {document.highlight}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {mediaId && (
        <div className="rounded-lg border border-primary/40 p-3">
          <div className="mb-2 flex items-center gap-2">
            <FileText className="h-4 w-4 text-primary" />
            <span className="truncate text-sm font-medium">
              {item.data?.title || t("knowledge.imaReading")}
            </span>
            <button
              type="button"
              onClick={() => setMediaId(null)}
              className="ml-auto rounded p-1 text-muted hover:bg-accent"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          {item.isLoading ? (
            <div className="flex justify-center py-3">
              <Loader2 className="h-4 w-4 animate-spin text-muted" />
            </div>
          ) : item.error ? (
            <p className="text-xs text-danger">
              {errorText(item.error, t("common.requestFailed"))}
            </p>
          ) : item.data?.text ? (
            <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words text-xs">
              {item.data.text}
            </pre>
          ) : (
            <p className="text-xs text-muted">{item.data?.note || t("knowledge.imaNoText")}</p>
          )}
          {item.data?.truncated && (
            <p className="mt-1 text-xs text-muted">{t("knowledge.imaTruncated")}</p>
          )}
        </div>
      )}
    </div>
  );
}

// ================= MarginNote =================

function MarginNotePanel() {
  const { t, lang } = useI18n();
  const devices = useMnDevices();
  const pair = useMnPair();
  const revoke = useMnRevoke();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("ipados");
  const [token, setToken] = useState("");
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [objectType, setObjectType] = useState("");
  const [selected, setSelected] = useState<MarginNoteObject | null>(null);
  const objects = useMnObjects(query, objectType);

  const pairDevice = async () => {
    setError(null);
    try {
      const result = await pair.mutateAsync({
        device_name: name.trim() || t("knowledge.mnDefaultDevice"),
        device_kind: kind,
      });
      setToken(result.token);
      setCopied(false);
      setName("");
    } catch (pairError) {
      setError(errorText(pairError, t("common.requestFailed")));
    }
  };

  const copyToken = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const revokeDevice = (deviceId: string, label: string) => {
    void confirmDialog({
      message: t("knowledge.mnRevokeConfirm", { name: label }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        revoke.mutate(deviceId);
      }
    });
  };

  const rows = objects.data?.results ?? objects.data?.objects ?? [];

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border/70 p-3">
        <div className="mb-2 flex items-center gap-2">
          <Smartphone className="h-4 w-4 text-muted" />
          <span className="text-sm font-medium">{t("knowledge.mnPairTitle")}</span>
          <span className="text-xs text-muted">{t("knowledge.mnPairDesc")}</span>
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t("knowledge.mnDeviceName")}
            className="min-w-0 flex-1 rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none focus:border-primary/60"
          />
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="rounded-lg border border-border bg-background/40 px-2 py-1.5 text-sm outline-none focus:border-primary/60"
          >
            <option value="ipados">iPadOS</option>
            <option value="ios">iOS</option>
            <option value="macos">macOS</option>
          </select>
          <button
            type="button"
            data-testid="mn-pair"
            disabled={pair.isPending}
            onClick={() => void pairDevice()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
          >
            {pair.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {t("knowledge.mnPair")}
          </button>
        </div>
        {error && <p className="mt-1 text-xs text-danger">{error}</p>}
        {token && (
          <div className="mt-2 rounded-lg border border-primary/40 bg-primary/5 p-2.5">
            <p className="text-xs font-medium">{t("knowledge.mnTokenTitle")}</p>
            <div className="mt-1 flex items-center gap-2">
              <code className="min-w-0 flex-1 break-all rounded bg-background/60 px-2 py-1 text-xs">
                {token}
              </code>
              <button
                type="button"
                onClick={() => void copyToken()}
                className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-2 py-1 text-xs transition-colors hover:bg-accent"
              >
                {copied ? <Check className="h-3 w-3" /> : <Clipboard className="h-3 w-3" />}
                {copied ? t("knowledge.readerCopied") : t("knowledge.mnCopy")}
              </button>
            </div>
            <p className="mt-1 text-xs text-muted">{t("knowledge.mnTokenHint")}</p>
          </div>
        )}
      </div>

      <div className="rounded-lg border border-border/70 p-3">
        <p className="mb-1 text-sm font-medium">{t("knowledge.mnDevices")}</p>
        {devices.isLoading ? (
          <div className="flex justify-center py-2">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : devices.error ? (
          <p className="text-xs text-danger">
            {errorText(devices.error, t("common.requestFailed"))}
          </p>
        ) : (devices.data?.devices.length ?? 0) === 0 ? (
          <p className="text-xs text-muted">{t("knowledge.mnNoDevices")}</p>
        ) : (
          <ul className="space-y-1">
            {devices.data?.devices.map((device) => (
              <li key={device.device_id} className="flex items-center gap-2 text-sm">
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    device.active ? "bg-success" : "bg-muted"
                  }`}
                />
                <span className="truncate">{device.device_name}</span>
                <span className="text-xs text-muted">{device.device_kind}</span>
                <span className="ml-auto text-xs text-muted">
                  {device.last_seen
                    ? t("knowledge.mnLastSeen", {
                        time: formatDateTime(Date.parse(device.last_seen) / 1000, lang),
                      })
                    : t("knowledge.mnNeverSeen")}
                </span>
                {device.active && (
                  <button
                    type="button"
                    title={t("knowledge.mnRevoke")}
                    onClick={() => revokeDevice(device.device_id, device.device_name)}
                    disabled={revoke.isPending}
                    className="rounded p-1 text-muted transition-colors hover:bg-accent hover:text-danger disabled:opacity-50"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="rounded-lg border border-border/70 p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Search className="h-3.5 w-3.5 text-muted" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("knowledge.mnSearchPlaceholder")}
            className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted"
          />
          <select
            value={objectType}
            onChange={(event) => setObjectType(event.target.value)}
            className="rounded-lg border border-border bg-background/40 px-2 py-1 text-xs outline-none focus:border-primary/60"
          >
            <option value="">{t("knowledge.mnAllTypes")}</option>
            {MN_TYPES.map((type) => (
              <option key={type} value={type}>
                {t(`knowledge.mnType_${type}`)}
              </option>
            ))}
          </select>
        </div>
        {objects.isLoading ? (
          <div className="flex justify-center py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : objects.error ? (
          <p className="text-xs text-danger">
            {errorText(objects.error, t("common.requestFailed"))}
          </p>
        ) : rows.length === 0 ? (
          <p className="text-xs text-muted">{t("knowledge.mnNoObjects")}</p>
        ) : (
          <ul className="max-h-64 space-y-0.5 overflow-y-auto">
            {rows.map((row) => (
              <li key={row.object_id}>
                <button
                  type="button"
                  onClick={() => setSelected(row)}
                  className="w-full rounded px-2 py-1.5 text-left transition-colors hover:bg-accent"
                >
                  <span className="flex items-center gap-1.5 text-sm">
                    <FileText className="h-3.5 w-3.5 shrink-0 text-muted" />
                    <span className="truncate">{row.title || row.object_id}</span>
                    <span className="ml-auto shrink-0 text-xs text-muted">
                      {t(`knowledge.mnType_${row.object_type}`)}
                    </span>
                  </span>
                  {(row.excerpt || row.content) && (
                    <span className="mt-0.5 block truncate text-xs text-muted">
                      {row.excerpt || row.content}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
        {selected && (
          <div className="mt-2 rounded-lg border border-primary/40 p-2.5">
            <div className="mb-1 flex items-center gap-2">
              <span className="truncate text-sm font-medium">{selected.title}</span>
              <span className="text-xs text-muted">
                {t(`knowledge.mnType_${selected.object_type}`)}
              </span>
              <button
                type="button"
                onClick={() => setSelected(null)}
                className="ml-auto rounded p-0.5 text-muted hover:bg-accent"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {selected.document_title && (
              <p className="text-xs text-muted">
                {selected.document_title}
                {selected.page != null
                  ? ` · ${t("knowledge.mnPage", { n: String(selected.page) })}`
                  : ""}
              </p>
            )}
            <p className="mt-1 whitespace-pre-wrap break-words text-xs">
              {selected.content || selected.excerpt || t("knowledge.mnNoContent")}
            </p>
            {selected.tags.length > 0 && (
              <p className="mt-1 text-xs text-muted">
                {t("knowledge.mnTags")}: {selected.tags.join(" · ")}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
