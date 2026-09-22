"use client";

/** 输入区（§7.21）：textarea（Enter 发送 / Shift+Enter 换行，中文输入法守卫）+ 附件上传 + 发送/停止。 */

import { useRef, useState } from "react";
import { CircleStop, Paperclip, Send, X } from "lucide-react";
import { useChatStore, type AttachmentRef } from "@/hooks/useChat";
import { useI18n } from "@/hooks/useI18n";
import { apiFetch } from "@/lib/api";

const MAX_FILES = 5;

export default function Composer() {
  const { t } = useI18n();
  const send = useChatStore((s) => s.send);
  const stop = useChatStore((s) => s.stop);
  const active = useChatStore((s) => s.active);
  const ensureSession = useChatStore((s) => s.ensureSession);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composingRef = useRef(false);

  const handleUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) {
      return;
    }
    setUploadError(null);
    setUploading(true);
    try {
      const sessionId = await ensureSession();
      for (const file of Array.from(files).slice(0, MAX_FILES - attachments.length)) {
        const form = new FormData();
        form.append("file", file);
        form.append("session_id", sessionId);
        const record = await apiFetch<AttachmentRef>("/api/v1/attachments", {
          method: "POST",
          body: form,
        });
        setAttachments((prev) => [...prev, { id: record.id, name: record.name, mime: record.mime }]);
      }
    } catch (error) {
      setUploadError(t("chat.uploadFailed", { msg: String(error) }));
    } finally {
      setUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  };

  const removeAttachment = async (attachment: AttachmentRef) => {
    setAttachments((prev) => prev.filter((a) => a.id !== attachment.id));
    void apiFetch(`/api/v1/attachments/${attachment.id}`, { method: "DELETE" }).catch(
      () => undefined
    );
  };

  const handleSubmit = async () => {
    if (!text.trim() && attachments.length === 0) {
      return;
    }
    const message = text;
    const refs = attachments;
    setText("");
    setAttachments([]);
    await send(message, refs);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !composingRef.current) {
      event.preventDefault();
      void handleSubmit();
    }
  };

  return (
    <div className="border-t border-border/70 px-4 py-3">
      <div className="mx-auto max-w-3xl">
        {(attachments.length > 0 || uploading) && (
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            {attachments.map((attachment) => (
              <span
                key={attachment.id}
                className="inline-flex items-center gap-1 rounded-full bg-accent/60 px-2.5 py-1 text-xs"
              >
                {attachment.name}
                <button
                  type="button"
                  onClick={() => void removeAttachment(attachment)}
                  className="text-muted transition-colors hover:text-danger"
                  aria-label={t("chat.removeAttachment")}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            {uploading && <span className="text-xs text-muted">{t("chat.uploading")}</span>}
          </div>
        )}
        {uploadError && <div className="mb-2 text-xs text-danger">{uploadError}</div>}
        <div className="flex items-end gap-2 rounded-2xl border border-border bg-surface p-2 focus-within:border-primary/50">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="rounded-lg p-2 text-muted transition-colors hover:bg-accent hover:text-foreground"
            title={t("chat.attachHint")}
            aria-label={t("chat.attach")}
          >
            <Paperclip className="h-4 w-4" />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(event) => void handleUpload(event.target.files)}
          />
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
            }}
            placeholder={t("chat.placeholder")}
            rows={1}
            className="max-h-40 min-h-[2.25rem] flex-1 resize-none bg-transparent py-1.5 text-sm outline-none placeholder:text-muted"
          />
          {active ? (
            <button
              type="button"
              onClick={stop}
              className="rounded-xl bg-danger/10 p-2 text-danger transition-colors hover:bg-danger/20"
              aria-label={t("chat.stop")}
            >
              <CircleStop className="h-4 w-4" />
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={!text.trim() && attachments.length === 0}
              className="rounded-xl bg-primary p-2 text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
              aria-label={t("chat.send")}
            >
              <Send className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
