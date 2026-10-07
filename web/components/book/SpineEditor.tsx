"use client";

/** 目录编辑器（draft 专用）：行内改章名（失焦/回车提交）、上下移、删章。
 *
 * 每次改动都把**全量有序列表**交给上层 PATCH（后端靠数组顺序认排序，靠缺 key
 * 认删除）；行内改名先落本地草稿，失焦才提交——打字过程不该每键一次请求。
 */

import { useState } from "react";
import { ArrowDown, ArrowUp, Trash2 } from "lucide-react";
import { confirmDialog } from "@/hooks/useConfirm";
import { useI18n } from "@/hooks/useI18n";
import {
  blockTypeKey,
  moveChapter,
  planTypeCounts,
  removeChapter,
  renameChapter,
} from "@/lib/book";
import type { BookChapter } from "@/types/api";

interface Props {
  chapters: BookChapter[];
  /** 有 PATCH 在飞（按钮禁掉，防连点互相覆盖）。 */
  busy: boolean;
  disabled: boolean;
  onCommit: (chapters: BookChapter[]) => void;
}

export default function SpineEditor({ chapters, busy, disabled, onCommit }: Props) {
  const { t } = useI18n();
  // 行内改名草稿：key → 正在敲的字（不在草稿里的行显示服务端值）
  const [typing, setTyping] = useState<Record<string, string>>({});

  const commitRename = (chapter: BookChapter) => {
    const value = typing[chapter.key];
    if (value === undefined) {
      return;
    }
    setTyping((prev) => {
      const next = { ...prev };
      delete next[chapter.key];
      return next;
    });
    const title = value.trim();
    if (!title || title === chapter.title) {
      return; // 空名/没改：原样落回服务端值（输入框自己会回去）
    }
    onCommit(renameChapter(chapters, chapter.key, title));
  };

  const handleDelete = (chapter: BookChapter) => {
    void confirmDialog({
      message: t("book.deleteChapterConfirm", { title: chapter.title }),
      danger: true,
    }).then((ok) => {
      if (ok) {
        onCommit(removeChapter(chapters, chapter.key));
      }
    });
  };

  return (
    <ul className="space-y-1.5" data-testid="bk-spine">
      {chapters.map((chapter, index) => {
        const counts = planTypeCounts(chapter.blocks_plan);
        const planText = Array.from(counts)
          .map(([type, count]) => `${t(blockTypeKey(type))}×${count}`)
          .join(" · ");
        return (
          <li
            key={chapter.key}
            data-testid="bk-chapter"
            data-key={chapter.key}
            className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2"
          >
            <span className="w-5 shrink-0 text-xs text-muted">{index + 1}</span>
            <input
              data-testid="bk-chapter-title"
              value={typing[chapter.key] ?? chapter.title}
              disabled={disabled}
              onChange={(event) =>
                setTyping((prev) => ({ ...prev, [chapter.key]: event.target.value }))
              }
              onBlur={() => commitRename(chapter)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.currentTarget.blur();
                } else if (event.key === "Escape") {
                  setTyping((prev) => {
                    const next = { ...prev };
                    delete next[chapter.key];
                    return next;
                  });
                }
              }}
              className="min-w-0 flex-1 rounded border border-transparent bg-transparent px-1.5 py-0.5 text-sm outline-none hover:border-border focus:border-primary/50"
            />
            <span className="hidden shrink-0 text-xs text-muted sm:block">
              {t("book.chapterBlocks", { n: String(chapter.blocks_plan.length) })}
              {planText ? ` · ${planText}` : ""}
            </span>
            <div className="flex shrink-0 items-center gap-0.5">
              <button
                type="button"
                data-testid="bk-chapter-up"
                disabled={disabled || busy || index === 0}
                onClick={() => onCommit(moveChapter(chapters, index, "up"))}
                title={t("book.moveUp")}
                className="rounded p-1 text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-30"
              >
                <ArrowUp className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                data-testid="bk-chapter-down"
                disabled={disabled || busy || index === chapters.length - 1}
                onClick={() => onCommit(moveChapter(chapters, index, "down"))}
                title={t("book.moveDown")}
                className="rounded p-1 text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-30"
              >
                <ArrowDown className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                data-testid="bk-chapter-delete"
                disabled={disabled || busy || chapters.length <= 1}
                onClick={() => handleDelete(chapter)}
                title={chapters.length <= 1 ? t("book.keepOneChapter") : t("book.deleteChapter")}
                className="rounded p-1 text-muted transition-colors hover:bg-accent hover:text-danger disabled:opacity-30"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
