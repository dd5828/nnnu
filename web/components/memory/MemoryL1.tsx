"use client";

/**
 * L1 轨迹浏览（§7.10）：按分面 × 月度文件翻 append-only 事件行，只读。
 *
 * 行不可删不可改（行号即 L2/L3 的引用锚点，删行会让引用 seq 校验全断——
 * 后端 409 read_only_layer 的同一条理由）。默认取最新月文件。
 */

import { useState } from "react";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useMemoryL1 } from "@/hooks/useMemory";
import { errorText } from "@/lib/errors";
import {
  eventBadgeClass,
  eventLabelKey,
  formatTs,
  l1Summary,
  previewText,
  surfaceLabelKey,
} from "@/lib/memory";
import ChipRow from "./Chips";

const PAGE_SIZE = 200;

export default function MemoryL1({ surfaces }: { surfaces: string[] }) {
  const { t } = useI18n();
  const [surface, setSurface] = useState(surfaces[0] ?? "chat");
  const [file, setFile] = useState(""); // 空串 = 最新月（后端默认）
  const [offset, setOffset] = useState(0);
  const page = useMemoryL1(surface, file, offset, PAGE_SIZE);

  // 换分面/换文件回第一页（在事件里重置，不用 effect 回写 state）
  const pickSurface = (value: string) => {
    setSurface(value);
    setFile("");
    setOffset(0);
  };

  const rows = page.data?.rows ?? [];
  const total = page.data?.total ?? 0;
  const files = page.data?.files ?? [];

  return (
    <div className="space-y-3">
      <ChipRow
        testId="l1-surfaces"
        items={surfaces.map((value) => ({ value, label: t(surfaceLabelKey(value)) }))}
        active={surface}
        onSelect={pickSurface}
      />
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <label htmlFor="l1-file" className="shrink-0">
          {t("memory.l1File")}
        </label>
        <select
          id="l1-file"
          data-testid="l1-file"
          value={file || files[files.length - 1] || ""}
          onChange={(event) => {
            setFile(event.target.value);
            setOffset(0);
          }}
          className="rounded-md border border-border bg-surface px-2 py-1 text-xs"
        >
          {files.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        <span className="flex-1" />
        <span data-testid="l1-total">{t("memory.l1Total", { n: String(total) })}</span>
      </div>

      {page.isLoading ? (
        <div className="flex justify-center py-10">
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        </div>
      ) : page.error ? (
        <p className="text-sm text-danger">{errorText(page.error, t("common.requestFailed"))}</p>
      ) : rows.length === 0 ? (
        <p
          data-testid="l1-empty"
          className="rounded-xl border border-dashed border-border py-10 text-center text-sm text-muted"
        >
          {t("memory.l1Empty")}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {rows.map((row, index) => (
            <li
              key={`${offset + index}`}
              data-testid="l1-row"
              data-event={row.event}
              className="flex items-start gap-2 rounded-lg border border-border bg-surface px-3 py-2"
            >
              <span
                className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] ${eventBadgeClass(
                  row.event
                )}`}
              >
                {t(eventLabelKey(row.event))}
              </span>
              <span className="min-w-0 flex-1 break-words text-xs">
                {previewText(l1Summary(row), 240)}
              </span>
              <span className="shrink-0 text-[10px] text-muted">{formatTs(row.ts)}</span>
            </li>
          ))}
        </ul>
      )}

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-center gap-3 text-xs">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 hover:bg-accent disabled:opacity-40"
          >
            <ChevronLeft className="size-3.5" />
            {t("memory.prevPage")}
          </button>
          <span className="text-muted">
            {t("memory.pageInfo", {
              from: String(Math.min(offset + 1, total)),
              to: String(Math.min(offset + PAGE_SIZE, total)),
              total: String(total),
            })}
          </span>
          <button
            type="button"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 hover:bg-accent disabled:opacity-40"
          >
            {t("memory.nextPage")}
            <ChevronRight className="size-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}
