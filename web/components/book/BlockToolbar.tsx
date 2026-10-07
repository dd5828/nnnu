"use client";

/** 块悬浮工具条：上移/下移/插入/编辑正文/重新生成/换类型/删除。
 *
 * 悬浮（或键盘聚焦）才显形，避免正文被按钮淹没；两个下拉（插入/换类型）共用
 * 一份「点外面关掉」监听（照笔记本菜单手法）。「编辑正文」只给 markdown 家族的
 * 块——update op 只吃这类 payload，其余类型只能重新生成或换类型。
 */

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Pencil, Plus, RefreshCw, Repeat, Trash2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { BLOCK_TYPES, blockTypeKey } from "@/lib/book";
import type { BlockType, BookBlock } from "@/types/api";
import type { BlockActions } from "@/components/book/BlockRenderer";

type Menu = "insert" | "retype" | null;

export default function BlockToolbar({
  block,
  actions,
}: {
  block: BookBlock;
  actions: BlockActions;
}) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<Menu>(null);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menu) {
      return;
    }
    const handler = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setMenu(null);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menu]);

  const pick = (type: BlockType) => {
    const current = menu;
    setMenu(null);
    if (current === "insert") {
      actions.onInsert(type);
    } else if (current === "retype") {
      actions.onRetype(type);
    }
  };

  const btn =
    "rounded p-1 text-muted transition-colors hover:bg-accent hover:text-foreground disabled:opacity-30";

  return (
    <div
      ref={ref}
      data-testid="bk-toolbar"
      className="absolute right-1.5 top-1.5 z-10 flex items-center gap-0.5 rounded-lg border border-border bg-surface p-0.5 opacity-0 shadow-sm transition-opacity group-focus-within:opacity-100 group-hover:opacity-100"
    >
      <button
        type="button"
        data-testid="bk-block-up"
        disabled={actions.busy || actions.index === 0}
        onClick={() => actions.onMove("up")}
        title={t("book.moveUp")}
        className={btn}
      >
        <ArrowUp className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        data-testid="bk-block-down"
        disabled={actions.busy || actions.index === actions.total - 1}
        onClick={() => actions.onMove("down")}
        title={t("book.moveDown")}
        className={btn}
      >
        <ArrowDown className="h-3.5 w-3.5" />
      </button>
      <span className="mx-0.5 h-4 w-px bg-border" />
      <button
        type="button"
        data-testid="bk-block-insert"
        disabled={actions.busy}
        onClick={() => setMenu((prev) => (prev === "insert" ? null : "insert"))}
        title={t("book.insertBlock")}
        className={btn}
      >
        <Plus className="h-3.5 w-3.5" />
      </button>
      {actions.canEdit && (
        <button
          type="button"
          data-testid="bk-block-edit"
          disabled={actions.busy || actions.editing}
          onClick={actions.onStartEdit}
          title={t("book.editBlock")}
          className={btn}
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
      )}
      {block.type !== "note" && (
        <button
          type="button"
          data-testid="bk-block-regenerate"
          disabled={actions.busy}
          onClick={actions.onRegenerate}
          title={t("book.regenerateBlock")}
          className={btn}
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      )}
      <button
        type="button"
        data-testid="bk-block-retype"
        disabled={actions.busy}
        onClick={() => setMenu((prev) => (prev === "retype" ? null : "retype"))}
        title={t("book.retypeBlock")}
        className={btn}
      >
        <Repeat className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        data-testid="bk-block-delete"
        disabled={actions.busy}
        onClick={actions.onDelete}
        title={t("book.deleteBlock")}
        className="rounded p-1 text-muted transition-colors hover:bg-accent hover:text-danger disabled:opacity-30"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>

      {menu && (
        <div
          data-testid="bk-type-menu"
          className="absolute right-1.5 top-full z-20 mt-1 max-h-64 w-36 overflow-y-auto rounded-lg border border-border bg-surface p-1 shadow-lg"
        >
          {BLOCK_TYPES.map((type) => (
            <button
              key={type}
              type="button"
              data-testid="bk-type-item"
              data-type={type}
              onClick={() => pick(type)}
              className="block w-full rounded px-2 py-1 text-left text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
            >
              {t(blockTypeKey(type))}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
