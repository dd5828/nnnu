/**
 * 文件名 → Prism 语言名（代码高亮用）。
 *
 * 只覆盖后端 CODE_EXTENSIONS 那 22 个扩展名（src/nnnu/services/files/service.py）。
 * 返回 null 表示「不是代码文件」，调用方按纯文本渲染。
 */

/** 扩展名（小写、带点）→ react-syntax-highlighter 的 Prism 语言名。 */
export const CODE_EXT_TO_LANG: Record<string, string> = {
  ".py": "python",
  ".js": "javascript",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".jsx": "jsx",
  ".java": "java",
  ".c": "c",
  ".h": "c",
  ".cpp": "cpp",
  ".hpp": "cpp",
  ".go": "go",
  ".rs": "rust",
  ".sh": "bash",
  ".sql": "sql",
  ".json": "json",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".toml": "toml",
  ".xml": "markup",
  ".html": "markup",
  ".css": "css",
  ".ipynb": "json", // notebook 本身就是 JSON，Prism 没有单独语法
};

/** 取最后一个点后的扩展名（小写、含点）；没有点则空串。 */
export function extOf(filename: string): string {
  const base = filename.toLowerCase().split(/[\\/]/).pop() ?? "";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot) : "";
}

/** 代码文件返回 Prism 语言名，否则 null。 */
export function langForFilename(filename: string): string | null {
  return CODE_EXT_TO_LANG[extOf(filename)] ?? null;
}
