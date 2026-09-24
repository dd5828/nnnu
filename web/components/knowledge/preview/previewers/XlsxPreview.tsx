"use client";

/**
 * XLSX 预览：exceljs 解析后自绘 HTML 表格（表格本身没什么库能画得比 <table> 更省）。
 *
 * 单元格只取显示文本：公式给缓存值，格式全丢——这是个快速瞄一眼的地方，不是编辑器。
 * 行/列数硬上限，超了给截断提示，完整内容走下载。
 */

import { useEffect, useState } from "react";
import { useI18n } from "@/hooks/useI18n";
import { useBinarySource } from "../useBinarySource";
import { PreviewFailure, PreviewSpinner } from "./PreviewStatus";

// 一个表可以塞几百万个格子，全画出来会锁死标签页
const MAX_ROWS = 1000;
const MAX_COLS = 60;

interface SheetModel {
  name: string;
  rows: string[][];
  truncated: boolean;
}

interface XlsxLoad {
  url: string;
  sheets: SheetModel[] | null;
  failed: boolean;
}

export default function XlsxPreview({ url }: { url: string }) {
  const { t } = useI18n();
  const src = useBinarySource(url);
  const [loaded, setLoaded] = useState<XlsxLoad | null>(null);
  const [active, setActive] = useState(0);

  // 结果自带 url 键：换了文档就当没解析过（loading），不在 effect 里重置
  const current = loaded && loaded.url === url ? loaded : null;
  const sheets = current?.sheets ?? null;
  const failed = current?.failed ?? false;

  useEffect(() => {
    if (src.kind !== "ready") return;

    let cancelled = false;
    (async () => {
      try {
        const mod = await import("exceljs");
        // 打包器对 default/named 的处理不一致，解包兜一层
        const ExcelJS = (mod as unknown as { default?: typeof mod }).default ?? mod;
        if (cancelled) return;
        const wb = new ExcelJS.Workbook();
        await wb.xlsx.load(src.buffer);
        if (cancelled) return;

        const parsed: SheetModel[] = wb.worksheets.map((ws) => {
          const colCount = Math.min(ws.columnCount || 0, MAX_COLS);
          const rows: string[][] = [];
          let truncated = ws.columnCount > MAX_COLS;
          ws.eachRow({ includeEmpty: true }, (row, rowNumber) => {
            if (rowNumber > MAX_ROWS) {
              truncated = true;
              return;
            }
            const cells: string[] = [];
            for (let c = 1; c <= colCount; c += 1) {
              const text = row.getCell(c).text;
              cells.push(typeof text === "string" ? text : String(text ?? ""));
            }
            rows.push(cells);
          });
          return { name: ws.name, rows, truncated };
        });
        if (cancelled) return;
        setLoaded({ url, sheets: parsed.length ? parsed : [], failed: false });
        setActive(0);
      } catch {
        if (!cancelled) {
          setLoaded({ url, sheets: null, failed: true });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [src, url]);

  if (src.kind === "error") {
    return <PreviewFailure code={src.code} />;
  }

  if (failed) {
    return (
      <div className="flex h-full items-center justify-center px-8 text-center text-xs text-muted">
        {t("knowledge.readerXlsxError")}
      </div>
    );
  }

  if (!sheets) {
    return <PreviewSpinner />;
  }

  if (sheets.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-8 text-center text-xs text-muted">
        {t("knowledge.readerNoSheets")}
      </div>
    );
  }

  const sheet = sheets[Math.min(active, sheets.length - 1)];

  return (
    <div className="flex h-full flex-col bg-surface">
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="border-collapse text-xs">
          <tbody>
            {sheet.rows.map((cells, r) => (
              <tr key={r}>
                <td className="sticky left-0 z-10 border border-border/50 bg-accent/55 px-2 py-1 text-right text-[10px] tabular-nums text-muted/70">
                  {r + 1}
                </td>
                {cells.map((cell, c) => (
                  <td
                    key={c}
                    className={`max-w-[280px] truncate border border-border/40 px-2 py-1 ${
                      r === 0 ? "bg-accent/35 font-medium" : "bg-surface"
                    }`}
                    title={cell}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {sheet.truncated && (
          <p className="px-3 py-2 text-[11px] text-muted/70">
            {t("knowledge.readerXlsxTruncated")}
          </p>
        )}
      </div>

      {/* 多工作表才有标签条 */}
      {sheets.length > 1 && (
        <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-t border-border/40 bg-accent/25 px-2 py-1.5">
          {sheets.map((s, i) => (
            <button
              key={`${s.name}-${i}`}
              type="button"
              onClick={() => setActive(i)}
              className={`shrink-0 rounded-md px-2.5 py-1 text-[11.5px] font-medium transition-colors ${
                i === active ? "bg-surface shadow-sm" : "text-muted hover:bg-accent"
              }`}
              title={s.name}
            >
              <span className="block max-w-[140px] truncate">{s.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
