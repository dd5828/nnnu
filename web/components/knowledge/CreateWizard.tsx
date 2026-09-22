"use client";

/**
 * 建库向导（§7.9）：填名字 → 逐文件上传 → 轮询到索引完成 → 进详情页。
 *
 * 上传走 XHR（要字节进度），索引进度走 manifest 轮询——两段进度条接在一起，
 * 用户看到的是「一直在动」。文件逐个传：串行比并发好读，进度条不会互相抢。
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, FolderPlus, Loader2, Upload, X } from "lucide-react";
import { useCreateKb, useKb, useUploadQueue } from "@/hooks/useKnowledge";
import { useI18n } from "@/hooks/useI18n";
import { DocStatusChip, formatSize, ProgressBar } from "./status";

export default function CreateWizard({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const router = useRouter();
  const create = useCreateKb();
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState<string | null>(null);

  const { data: kb } = useKb(createdId);
  const { uploads, busy, enqueue } = useUploadQueue(createdId);
  const allUploaded = uploads.every((item) => item.done); // 没选文件也算传完了
  const ready = kb?.status === "ready" && !kb.build;

  // 建完并索引完就进详情页：向导的尽头是这个库本身
  useEffect(() => {
    if (createdId && ready && allUploaded) {
      router.push(`/knowledge/${createdId}`);
    }
  }, [createdId, ready, allUploaded, router]);

  // 先取出文件再清 value：FileList 是活的，清 value 会把里面的文件也清掉
  const addFiles = (picked: File[]) => {
    setFiles((prev) => [...prev, ...picked]);
    setError(null);
  };

  const handleSubmit = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError(t("knowledge.nameRequired"));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const manifest = await create.mutateAsync(trimmed);
      setCreatedId(manifest.id);
      await enqueue(files, manifest.id); // 库刚建出来，hook 手上还没 id，显式传
    } catch (createError) {
      setError(String(createError));
    } finally {
      setCreating(false);
    }
  };

  const started = createdId !== null;
  const working = creating || busy;

  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="mb-4 flex items-center gap-2">
        <FolderPlus className="h-4 w-4 text-primary" />
        <h2 className="text-base font-semibold">{t("knowledge.newKb")}</h2>
        <button
          type="button"
          onClick={onClose}
          title={t("common.close")}
          className="ml-auto rounded-lg p-1 text-muted transition-colors hover:bg-accent"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-4">
        <div>
          <label htmlFor="kb-name" className="mb-1 block text-sm font-medium">
            {t("knowledge.nameLabel")}
          </label>
          <input
            id="kb-name"
            data-testid="kb-name"
            className="w-full rounded-lg border border-border bg-background/40 px-3 py-1.5 text-sm outline-none transition-colors focus:border-primary/60 disabled:opacity-60"
            placeholder={t("knowledge.namePlaceholder")}
            value={name}
            disabled={started}
            onChange={(e) => {
              setName(e.target.value);
              setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !started && !busy) {
                void handleSubmit();
              }
            }}
          />
        </div>

        <div>
          <div className="mb-1 flex items-baseline gap-2">
            <span className="text-sm font-medium">{t("knowledge.filesLabel")}</span>
            <span className="text-xs text-muted">{t("knowledge.filesHint")}</span>
          </div>
          <input
            data-testid="kb-file-input"
            type="file"
            multiple
            disabled={started}
            onChange={(e) => {
              const picked = Array.from(e.target.files ?? []);
              e.target.value = ""; // 允许重复选同一个文件
              addFiles(picked);
            }}
            className="block w-full cursor-pointer rounded-lg border border-dashed border-border bg-background/40 px-3 py-2 text-xs file:mr-3 file:rounded-md file:border-0 file:bg-accent file:px-2.5 file:py-1 file:text-xs file:text-foreground"
          />
          {files.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-muted">
              {files.map((file, index) => (
                <li key={`${file.name}-${index}`} className="flex items-center gap-2">
                  <span className="truncate">{file.name}</span>
                  <span className="shrink-0">{formatSize(file.size)}</span>
                  {!started && (
                    <button
                      type="button"
                      onClick={() => setFiles((prev) => prev.filter((_, i) => i !== index))}
                      className="shrink-0 rounded p-0.5 hover:bg-accent hover:text-danger"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        {started && (uploads.length > 0 || kb) && (
          <div data-testid="kb-create-progress" className="space-y-2 rounded-lg bg-accent/40 p-3">
            {uploads.map((item, index) => (
              <div key={`${item.name}-${index}`} className="text-xs">
                <div className="flex items-center gap-2">
                  {item.error ? (
                    <span className="text-danger">{item.error}</span>
                  ) : item.done ? (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
                  ) : (
                    <Upload className="h-3.5 w-3.5 shrink-0 text-primary" />
                  )}
                  <span className="truncate">{item.name}</span>
                  <span className="ml-auto shrink-0 text-muted">
                    {item.done
                      ? formatSize(item.size)
                      : t("knowledge.uploading", {
                          percent: String(Math.round(item.ratio * 100)),
                        })}
                  </span>
                </div>
                {!item.done && !item.error && (
                  <div className="mt-1">
                    <ProgressBar value={item.ratio} />
                  </div>
                )}
              </div>
            ))}
            {allUploaded && kb && (
              <div className="border-t border-border/60 pt-2">
                <div className="flex items-center gap-2 text-xs">
                  {ready ? (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
                  ) : (
                    <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />
                  )}
                  <span className="truncate">
                    {ready ? t("knowledge.created") : kb.build?.note || t("knowledge.indexingHint")}
                  </span>
                  <span className="ml-auto shrink-0 text-muted">
                    {kb.docs
                      .filter((doc) => doc.status !== "deleted")
                      .map((doc) => (
                        <DocStatusChip key={doc.doc_id} status={doc.status} />
                      ))}
                  </span>
                </div>
                {!ready && (
                  <div className="mt-1">
                    <ProgressBar value={kb.build?.progress ?? 0} />
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {error && <p className="text-xs text-danger">{error}</p>}

        <div className="flex items-center gap-2 border-t border-border/60 pt-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-sm text-muted transition-colors hover:bg-accent"
          >
            {started ? t("common.close") : t("knowledge.cancel")}
          </button>
          {!started && (
            <button
              type="button"
              data-testid="kb-create-submit"
              onClick={() => void handleSubmit()}
              disabled={working}
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-1.5 text-sm text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-50"
            >
              {working && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {working ? t("knowledge.creating") : t("knowledge.create")}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
