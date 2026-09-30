/** 渲染围栏的前端解析（§7.7 / §7.8 正文契约，后端 counterpart 在
 *  `src/nnnu/services/render/validation.py` 与 `animator.py::_assemble_body`——改一边记得同步另一边）。
 *
 * 正文契约：
 * - visualize 正文 = 标题 + 一句说明 + 恰好一个渲染围栏（```svg / ```echarts / ```mermaid / ```html）；
 * - math_animator 正文 = 标题 + 总结 + ```nnnu-artifact（JSON 载荷）+ ```python 源码 + 日志节选。
 *
 * 这里刻意比后端严：后端抠代码时知道目标渲染类型，认得 json 这类别名；前端是从一段任意
 * markdown 里认围栏，只认恰好那四个语言标记，免得把用户正文里的 ```json 误当成 echarts 选项。
 */

export const RENDER_KINDS = ["svg", "echarts", "mermaid", "html"] as const;
export type RenderKind = (typeof RENDER_KINDS)[number];

/** 二进制产物围栏的语言标记（内容是一段 JSON，见 ArtifactInfo）。 */
export const ARTIFACT_LANG = "nnnu-artifact";

/** 每种渲染块下载时的文件后缀。 */
export const RENDER_FILE_EXT: Record<RenderKind, string> = {
  svg: "svg",
  echarts: "json",
  mermaid: "mmd",
  html: "html",
};

export interface Fence {
  lang: string;
  code: string;
}

export interface RenderBlock {
  kind: RenderKind;
  code: string;
}

export interface ArtifactInfo {
  url: string;
  filename: string;
  mime: string;
  kind: string;
  attempts: number;
}

/** 语言标记 → 渲染类型；不是四个之一返回 null。 */
export function kindFromLang(lang: string): RenderKind | null {
  return (RENDER_KINDS as readonly string[]).includes(lang) ? (lang as RenderKind) : null;
}

export function isRenderLang(lang: string): boolean {
  return kindFromLang(lang) !== null;
}

export function isArtifactLang(lang: string): boolean {
  return lang === ARTIFACT_LANG;
}

/** 扫出所有围栏（按行扫，容忍未闭合的最后一个：吃到文末）。 */
export function fences(text: string): Fence[] {
  const out: Fence[] = [];
  let lang: string | null = null;
  let buffer: string[] = [];
  for (const line of text.split("\n")) {
    if (lang === null) {
      const open = /^ {0,3}```([^\s`]*)\s*$/.exec(line);
      if (open) {
        lang = open[1].toLowerCase();
        buffer = [];
      }
      continue;
    }
    if (/^ {0,3}```\s*$/.test(line)) {
      out.push({ lang, code: buffer.join("\n") });
      lang = null;
      continue;
    }
    buffer.push(line);
  }
  if (lang !== null) {
    out.push({ lang, code: buffer.join("\n") });
  }
  return out;
}

/** 正文里所有渲染块（按出现顺序）。 */
export function renderBlocks(text: string): RenderBlock[] {
  const out: RenderBlock[] = [];
  for (const fence of fences(text)) {
    const kind = kindFromLang(fence.lang);
    if (kind !== null) {
      out.push({ kind, code: fence.code.trim() });
    }
  }
  return out;
}

/** 正文里第一个渲染块（聊天/笔记本详情用；拆不出返回 null，调用方照常 Markdown 兜底）。 */
export function firstRenderBlock(text: string): RenderBlock | null {
  return renderBlocks(text)[0] ?? null;
}

/** 正文里最后一个渲染块（playground 用：看最新一帧）。 */
export function lastRenderBlock(text: string): RenderBlock | null {
  const blocks = renderBlocks(text);
  return blocks.length > 0 ? blocks[blocks.length - 1] : null;
}

/** nnnu-artifact 围栏体（JSON 文本）→ ArtifactInfo；缺关键字段或解析不了返回 null。 */
export function parseArtifactJson(json: string): ArtifactInfo | null {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  const url = typeof record.url === "string" ? record.url : "";
  if (!url) {
    return null;
  }
  const attempts = typeof record.attempts === "number" ? record.attempts : 0;
  return {
    url,
    filename: typeof record.filename === "string" ? record.filename : "",
    mime: typeof record.mime === "string" ? record.mime : "",
    kind: typeof record.kind === "string" ? record.kind : "",
    attempts,
  };
}

/** 从一段 markdown 里找 nnnu-artifact 围栏并解析（聊天正文用）。 */
export function parseArtifact(text: string): ArtifactInfo | null {
  for (const fence of fences(text)) {
    if (isArtifactLang(fence.lang)) {
      return parseArtifactJson(fence.code);
    }
  }
  return null;
}

/** 把渲染块还原成带围栏的 markdown（存笔记本、复制、下载三处共用一份口径）。 */
export function toFenced(kind: RenderKind, code: string): string {
  return "```" + kind + "\n" + code + "\n```";
}
