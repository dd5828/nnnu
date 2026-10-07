"use client";

/**
 * Co-Writer 工作台（§7.13）：左边写、右边预览，选中一段文字让 AI 改写。
 *
 * 三条最容易踩的编排线（改动前先读三遍）：
 * 1. 自动保存走**串行链**：防抖只决定「什么时候发」，链子保证「按什么顺序发」——
 *    PATCH 乱序会让后到的旧正文把新正文盖回去；
 * 2. 发起改写前必须 flush 防抖并等链子清空：服务端拿它自己那份正文做切片校验，
 *    前端算的选区必须对它那份成立，否则必 409；
 * 3. accept 前必须 cancel 防抖：2 秒前挂起的那次 PATCH 拿的是改写前正文，
 *    不取消就会在写回之后把刚应用的结果冲掉。
 *
 * 预览吃的是 savedContent（最近一次服务端确认过的正文）：Markdown/Mermaid 整篇
 * 重渲很贵，不值得跟着每个键跑；accept 写回时两处一起更新，看起来仍是即时的。
 *
 * 结构上是「外壳取数 + 内层 keyed 工作台」：本地正文一旦装载就由内层状态接管，
 * 外壳重拉（accept 后失效缓存会有一轮）不能把它盖回去；换文档时 key 变化强制
 * 重挂载，草稿恢复等初始化逻辑只在挂载时跑一次（不在 effect 里 setState）。
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Loader2 } from "lucide-react";
import Markdown from "@/components/chat/Markdown";
import SaveToNotebook from "@/components/chat/SaveToNotebook";
import EditDiffDialog from "@/components/co-writer/EditDiffDialog";
import SelectionToolbar, {
  type CoWriterRunInput,
  type CoWriterSelection,
} from "@/components/co-writer/SelectionToolbar";
import {
  useAcceptCoWriterEdit,
  useCoWriterDoc,
  useCoWriterEdit,
  useRejectCoWriterEdit,
  useSaveCoWriterDoc,
} from "@/hooks/useCoWriter";
import { useI18n } from "@/hooks/useI18n";
import { ApiError } from "@/lib/api";
import {
  AUTOSAVE_DELAY_MS,
  applyEdit,
  codePointIndex,
  draftKey,
  parseDraft,
  resolveDraft,
  serializeDraft,
  utf16Index,
} from "@/lib/co-writer";
import { createDebounce, type Debounced } from "@/lib/debounce";
import { errorText } from "@/lib/errors";
import type { CoWriterDocDetail, CoWriterEditResult } from "@/types/api";

type SaveState = "idle" | "saving" | "saved" | "error";

/** 待确认的改写：result 来自服务端，start/end 是发起时的码点选区（本地写回用）。 */
interface PendingEdit {
  result: CoWriterEditResult;
  start: number;
  end: number;
}

// localStorage 草稿镜像：private 模式/配额满会抛，镜像坏了不能拦住写作（自动保存还在）
function readDraft(id: string): string | null {
  try {
    return localStorage.getItem(draftKey(id));
  } catch {
    return null;
  }
}

function writeDraft(id: string, content: string) {
  try {
    localStorage.setItem(draftKey(id), serializeDraft({ content, savedAt: Date.now() }));
  } catch {
    // 镜像写不进去就算了，PATCH 那条线不受影响
  }
}

function clearDraft(id: string) {
  try {
    localStorage.removeItem(draftKey(id));
  } catch {
    // 同上
  }
}

export default function CoWriterDocPage() {
  const { t } = useI18n();
  const params = useParams<{ id: string }>();
  const id = params?.id ?? "";
  const { data, isLoading, error } = useCoWriterDoc(id || null);

  if (!id || (!isLoading && (error || !data))) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-3">
        <p className="text-sm text-muted">{t("coWriter.notFound")}</p>
        <Link
          href="/co-writer"
          className="inline-flex items-center gap-1 text-xs text-muted transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {t("coWriter.backToList")}
        </Link>
      </main>
    );
  }

  if (!data) {
    return (
      <main className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </main>
    );
  }

  return <CoWriterWorkspace key={data.id} doc={data} />;
}

function CoWriterWorkspace({ doc }: { doc: CoWriterDocDetail }) {
  const { t, lang } = useI18n();
  const id = doc.id;

  const saveMutation = useSaveCoWriterDoc();
  const edit = useCoWriterEdit();
  const accept = useAcceptCoWriterEdit();
  const reject = useRejectCoWriterEdit();

  // 初始正文：有和服务端不一样的草稿就用草稿（并在顶栏下方给一条回退提示）
  const [restored] = useState(() => resolveDraft(doc.content, parseDraft(readDraft(doc.id))));
  const [content, setContent] = useState(restored.content);
  const [savedContent, setSavedContent] = useState(doc.content);
  const [title, setTitle] = useState(doc.title);
  const [draftRestored, setDraftRestored] = useState(restored.fromDraft);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [selection, setSelection] = useState<CoWriterSelection | null>(null);
  const [pending, setPending] = useState<PendingEdit | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // 只在事件/回调里读写的几个最新值（计时器到点时闭包可能已经过期）
  const contentRef = useRef(content);
  const titleRef = useRef(title);
  const savedTitleRef = useRef(doc.title);
  const saveChain = useRef<Promise<void>>(Promise.resolve());
  const debounceRef = useRef<Debounced<[string]> | null>(null);
  const saveLatestRef = useRef<(text: string) => void>(() => undefined);

  /** 串行排队一次 PATCH；正文存成功后顺手更新预览与草稿镜像。 */
  const enqueueSave = (body: { title?: string; content?: string }): Promise<void> => {
    const task = saveChain.current
      .catch(() => undefined) // 前一次失败别把链子掐死，后面的照样发
      .then(async () => {
        setSaveState("saving");
        try {
          await saveMutation.mutateAsync({ id, ...body });
          if (body.content !== undefined) {
            setSavedContent(body.content);
            if (contentRef.current === body.content) {
              clearDraft(id); // 这份正文还是当前正文，镜像才敢清
            }
          }
          setSaveState("saved");
        } catch {
          setSaveState("error");
        }
      });
    saveChain.current = task;
    return task;
  };

  // 防抖只建一次，回调要读「最新」的打包逻辑 → 每次渲染后把最新的塞进 ref
  useEffect(() => {
    saveLatestRef.current = (text: string) => {
      const patch: { title?: string; content: string } = { content: text };
      if (!titleRef.current.trim()) {
        patch.title = ""; // 空标题 = 交给服务端按正文首行取（输入框 placeholder 的语义）
      }
      void enqueueSave(patch);
    };
  });

  // 切页/关页时把欠着的自动保存发出去（镜像已在，只是别再多欠一次）
  useEffect(() => {
    const debounce = createDebounce<[string]>(
      (text) => saveLatestRef.current(text),
      AUTOSAVE_DELAY_MS
    );
    debounceRef.current = debounce;
    return () => {
      debounce.flush();
      debounceRef.current = null;
    };
  }, []);

  const syncSelection = () => {
    const el = textareaRef.current;
    if (!el) {
      return;
    }
    const { selectionStart, selectionEnd } = el;
    if (selectionStart === selectionEnd) {
      setSelection(null);
      return;
    }
    const start = codePointIndex(el.value, selectionStart);
    const end = codePointIndex(el.value, selectionEnd);
    const preview = Array.from(el.value.slice(selectionStart, selectionEnd)).slice(0, 40).join("");
    setSelection({ start, end, length: end - start, preview });
  };

  const handleContentChange = (text: string) => {
    setContent(text);
    contentRef.current = text;
    writeDraft(id, text); // 每次按键都落镜像：刷新/崩溃丢的是 PATCH，不是字
    debounceRef.current?.call(text);
  };

  const commitTitle = () => {
    if (titleRef.current === savedTitleRef.current) {
      return;
    }
    savedTitleRef.current = titleRef.current;
    void enqueueSave({ title: titleRef.current });
  };

  const runEdit = async (input: CoWriterRunInput) => {
    if (!selection) {
      return;
    }
    const start16 = utf16Index(content, selection.start);
    const end16 = utf16Index(content, selection.end);
    const original = content.slice(start16, end16);
    setEditError(null);
    try {
      debounceRef.current?.flush(); // 先把挂着的自动保存发出去
      await saveChain.current; // 等所有 PATCH 落地，再让服务端按新正文校验切片
      const result = await edit.mutateAsync({
        docId: id,
        start: selection.start,
        end: selection.end,
        original,
        action: input.action,
        instruction: input.instruction,
        language: lang,
        kbIds: input.kbIds,
        useWeb: input.useWeb,
      });
      setResolveError(null);
      setPending({ result, start: selection.start, end: selection.end });
    } catch (err) {
      setEditError(
        err instanceof DOMException && err.name === "TimeoutError"
          ? t("coWriter.editTimeout")
          : errorText(err, t("common.requestFailed"))
      );
    }
  };

  const handleAccept = async () => {
    if (!pending) {
      return;
    }
    setResolveError(null);
    debounceRef.current?.cancel(); // 关键：别让改写前挂起的 PATCH 冲掉刚写回的结果
    try {
      await accept.mutateAsync({ docId: id, editId: pending.result.edit_id });
    } catch (err) {
      setResolveError(
        err instanceof ApiError && err.code === "doc_changed"
          ? t("coWriter.docChanged")
          : err instanceof ApiError && err.code === "edit_expired"
            ? t("coWriter.editExpired")
            : errorText(err, t("common.requestFailed"))
      );
      return;
    }
    const next = applyEdit(contentRef.current, pending.start, pending.end, pending.result.edited);
    contentRef.current = next;
    setContent(next);
    setSavedContent(next);
    clearDraft(id);
    setPending(null);
    setSelection(null);
    setSaveState("saved");
  };

  const handleReject = () => {
    if (!pending) {
      return;
    }
    // 放弃失败了也不拦着关弹层：服务端那份 30 分钟自己过期，用户意图是「别改」
    void reject.mutateAsync({ docId: id, editId: pending.result.edit_id }).catch(() => undefined);
    setPending(null);
    setResolveError(null);
    setSelection(null);
  };

  const frozen = edit.isPending || pending !== null;

  return (
    <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        data-testid="cw-topbar"
        className="flex items-center gap-3 border-b border-border px-4 py-2.5"
      >
        <Link
          href="/co-writer"
          title={t("coWriter.backToList")}
          className="shrink-0 rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <input
          data-testid="cw-doc-title"
          value={title}
          onChange={(event) => {
            setTitle(event.target.value);
            titleRef.current = event.target.value;
          }}
          onBlur={commitTitle}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.currentTarget.blur();
            }
          }}
          placeholder={t("coWriter.titlePlaceholder")}
          className="min-w-0 flex-1 bg-transparent text-sm font-medium outline-none placeholder:font-normal placeholder:text-muted"
        />
        {saveState === "saving" && (
          <span className="inline-flex shrink-0 items-center gap-1 text-xs text-muted">
            <Loader2 className="h-3 w-3 animate-spin" />
            {t("coWriter.saving")}
          </span>
        )}
        {saveState === "saved" && (
          <span className="shrink-0 text-xs text-muted">{t("coWriter.saved")}</span>
        )}
        {saveState === "error" && (
          <span className="shrink-0 text-xs text-danger">{t("coWriter.saveFailed")}</span>
        )}
        <div className="shrink-0">
          <SaveToNotebook
            content={savedContent}
            recordType="co_writer"
            sourceRef={null}
            menuPlacement="down"
          />
        </div>
      </div>

      {draftRestored && (
        <div className="flex items-center gap-3 border-b border-border bg-accent px-4 py-1.5 text-xs">
          <span className="text-muted">{t("coWriter.draftRestored")}</span>
          <button
            type="button"
            data-testid="cw-draft-revert"
            onClick={() => {
              setContent(savedContent);
              contentRef.current = savedContent;
              clearDraft(id);
              setDraftRestored(false);
              setSaveState("saved");
            }}
            className="rounded border border-border px-2 py-0.5 transition-colors hover:bg-surface"
          >
            {t("coWriter.draftRevert")}
          </button>
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-2">
        <div className="flex min-h-0 flex-col border-b border-border md:border-b-0 md:border-r">
          <SelectionToolbar
            selection={selection}
            busy={edit.isPending}
            onRun={(input) => void runEdit(input)}
          />
          {editError && (
            <p data-testid="cw-error" className="px-3 pt-2 text-xs text-danger">
              {editError}
            </p>
          )}
          <textarea
            ref={textareaRef}
            data-testid="cw-editor"
            aria-label={t("coWriter.editor")}
            value={content}
            readOnly={frozen}
            spellCheck={false}
            onChange={(event) => handleContentChange(event.target.value)}
            onSelect={syncSelection}
            onKeyUp={syncSelection}
            onMouseUp={syncSelection}
            className="min-h-0 flex-1 resize-none bg-transparent p-3 font-mono text-sm leading-relaxed outline-none"
          />
        </div>
        <div
          data-testid="cw-preview"
          role="region"
          aria-label={t("coWriter.preview")}
          className="no-scrollbar min-h-0 overflow-y-auto p-4"
        >
          <Markdown text={savedContent} />
        </div>
      </div>

      {pending && (
        <EditDiffDialog
          result={pending.result}
          busy={accept.isPending}
          error={resolveError}
          onAccept={() => void handleAccept()}
          onReject={handleReject}
        />
      )}
    </main>
  );
}
