/** SVG 消毒（§7.7）：内联进页面前把可执行面剔干净。
 *
 * 模型产出的 SVG 走 `dangerouslySetInnerHTML` 直接进文档，等于把一段任意 XML 挂到
 * 页面里——`<script>`、`on*` 事件、`javascript:` 链接都得先剥掉。策略是白名单式的最小
 * 手术：
 * 1. 解析 DOM（解析失败直接抛，调用方给内联错误卡）；根元素不是 `<svg>` 也抛；
 * 2. 删掉可执行/可外联元素：script / foreignObject / iframe / object / embed / link /
 *    meta / style（style 连带能覆盖全页的 CSS 一起去掉）；
 * 3. 逐元素删 `on*` 事件属性；href / xlink:href / src 只放行安全 URL（同文档 # 锚点、
 *    站内 / 路径、http(s)、data:image 位图——data:image/svg+xml 不放行，它能把脚本藏进
 *    第二层）；
 * 4. id 统一加实例前缀，并同步改写 `url(#id)` 与 `href="#id"` 引用——同一页多张图时
 *    否则会互相串号（第二个 `<defs id="arrow">` 覆盖掉第一个）。
 *
 * 不追求「保留一切合法 SVG」：拿不准就删，删了顶多图差点意思，留着可能是 XSS。
 */

const BLOCKED_ELEMENTS = new Set([
  "script",
  "foreignobject",
  "iframe",
  "object",
  "embed",
  "link",
  "meta",
  "style",
  "base",
  "form",
  "input",
  "button",
]);

const URL_ATTRS = new Set(["href", "xlink:href", "src", "srcset", "action", "formaction"]);

const SAFE_DATA_URL = /^data:image\/(?:png|jpe?g|gif|webp);base64,/i;

function isSafeUrl(value: string): boolean {
  const text = value.trim();
  if (text === "") {
    return true; // 空值无害，留着
  }
  if (text.startsWith("#")) {
    return true; // 同文档引用（箭头 marker 等）
  }
  if (/^(?:https?:)?\/\//i.test(text) || text.startsWith("/") || text.startsWith("./")) {
    return true;
  }
  if (SAFE_DATA_URL.test(text)) {
    return true;
  }
  return false;
}

/** 前缀里的字符要能安全出现在 XML id 里（React useId 的 ":" 之类换掉）。 */
export function cleanPrefix(prefix: string): string {
  const cleaned = prefix.replace(/[^a-zA-Z0-9_-]/g, "-");
  return cleaned.startsWith("-") ? `s${cleaned}` : cleaned || "s";
}

/**
 * 消毒并序列化一张 SVG。
 * @param svg 原始 SVG 文本
 * @param prefix id 前缀（同一页多张图用不同值；React 侧传 useId 处理过的串）
 * @throws 解析失败或根元素不是 svg 时抛 Error
 */
export function sanitizeSvg(svg: string, prefix: string): string {
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  if (doc.getElementsByTagName("parsererror").length > 0 || !doc.documentElement) {
    throw new Error("SVG 解析失败");
  }
  const root = doc.documentElement;
  if (root.tagName.toLowerCase() !== "svg") {
    throw new Error("根元素不是 <svg>");
  }

  // 1) 危险元素：删（用快照遍历，边删边查活集合会漏）
  for (const element of Array.from(root.getElementsByTagName("*"))) {
    if (BLOCKED_ELEMENTS.has(element.tagName.toLowerCase())) {
      element.remove();
    }
  }

  // 2) 事件属性与危险 URL
  const elements: Element[] = [root, ...Array.from(root.getElementsByTagName("*"))];
  for (const element of elements) {
    for (const attr of Array.from(element.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) {
        element.removeAttribute(attr.name);
        continue;
      }
      if (URL_ATTRS.has(name) && !isSafeUrl(attr.value)) {
        element.removeAttribute(attr.name);
      }
    }
  }

  // 3) id 去重：先收集替换表，再统一改写 id 与引用
  const idMap = new Map<string, string>();
  for (const element of elements) {
    const id = element.getAttribute("id");
    if (!id) {
      continue;
    }
    // 同名 id 只记第一次（后面的本来就是重复 id，会把引用指到自己身上）
    if (idMap.has(id)) {
      element.setAttribute("id", idMap.get(id) as string);
      continue;
    }
    const next = `${cleanPrefix(prefix)}-${id}`;
    idMap.set(id, next);
    element.setAttribute("id", next);
  }
  if (idMap.size > 0) {
    const rewriteUrlRefs = (value: string): string =>
      value.replace(/url\(\s*(['"]?)#([^)'"\s]+)\1\s*\)/g, (match, quote: string, id: string) => {
        const next = idMap.get(id);
        return next ? `url(${quote}#${next}${quote})` : match;
      });
    const rewriteHashRef = (value: string): string => {
      if (!value.startsWith("#")) {
        return value;
      }
      const next = idMap.get(value.slice(1));
      return next ? `#${next}` : value;
    };
    for (const element of elements) {
      for (const attr of Array.from(element.attributes)) {
        let value = attr.value;
        if (value.includes("url(#")) {
          value = rewriteUrlRefs(value);
        }
        if (value.startsWith("#")) {
          value = rewriteHashRef(value);
        }
        if (value !== attr.value) {
          element.setAttribute(attr.name, value);
        }
      }
    }
  }

  return new XMLSerializer().serializeToString(root);
}
