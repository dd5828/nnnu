/** 纯前端存盘（§7.6 导出 / §7.7 渲染块下载）：正文就在手上，不绕后端。
 *
 * Blob + 临时 <a download> 是浏览器里唯一的「不问自答」存盘姿势；产物（视频/图片）
 * 例外——它在服务端，直接给 `<a href="{url}?download=1">` 链接走附件的 Content-Disposition。
 */

/** 存一段文本（默认按 UTF-8 文本处理）。 */
export function downloadText(
  text: string,
  filename: string,
  mime = "text/plain;charset=utf-8"
): void {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** 导出 Markdown（§7.6）：文件名 = 词干 + 日期。 */
export function downloadMarkdown(text: string, stem: string): void {
  downloadText(
    text,
    `${stem}-${new Date().toISOString().slice(0, 10)}.md`,
    "text/markdown;charset=utf-8"
  );
}
