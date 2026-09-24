/**
 * 知识中心阅读器的类型分派：决定一份文档用哪个预览器渲染。
 *
 * 只按「扩展名优先、MIME 兜底」判断，不看后端 /content 的 kind——那个字段
 * 只有 text/image 两种（docx/xlsx/md 解析出来都是 text），分不出渲染器。
 */

import { extOf, langForFilename } from "./langForFilename";

export type PreviewKind =
  | "pdf"
  | "markdown"
  | "code"
  | "text"
  | "docx"
  | "xlsx"
  | "office-text" // pptx 等二进制 Office：没有浏览器渲染器，退回解析文本
  | "fallback";

export interface FilePreviewSource {
  filename: string;
  /** KbDoc.mime；文件名没扩展名时靠它兜底。 */
  mimeType?: string;
}

// 浏览器有忠实渲染器的 OOXML 格式（docx-preview / exceljs）
const DOCX_EXTS = new Set([".docx", ".docm"]);
const XLSX_EXTS = new Set([".xlsx", ".xlsm"]);
// 没有可靠浏览器渲染器的 Office 二进制（含旧版非 OOXML 格式）→ 解析文本
const OFFICE_BINARY_EXTS = new Set([".pptx", ".ppt", ".doc", ".xls"]);
const MARKDOWN_EXTS = new Set([".md", ".markdown", ".rst"]);
const PLAIN_TEXT_EXTS = new Set([".txt", ".text", ".log", ".csv", ".tsv", ".env", ".conf"]);

export function previewKindFor(source: FilePreviewSource): PreviewKind {
  const ext = extOf(source.filename);
  const mime = source.mimeType ?? "";

  if (ext === ".pdf" || mime === "application/pdf") return "pdf";
  if (MARKDOWN_EXTS.has(ext) || mime === "text/markdown") return "markdown";
  if (
    DOCX_EXTS.has(ext) ||
    mime === "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  ) {
    return "docx";
  }
  if (
    XLSX_EXTS.has(ext) ||
    mime === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  ) {
    return "xlsx";
  }
  if (OFFICE_BINARY_EXTS.has(ext)) return "office-text";
  // 代码扩展名（含 .ipynb）——排在纯文本前，否则 text/plain 的 mime 会先命中
  if (langForFilename(source.filename)) return "code";
  if (PLAIN_TEXT_EXTS.has(ext) || mime.startsWith("text/")) return "text";
  return "fallback";
}
