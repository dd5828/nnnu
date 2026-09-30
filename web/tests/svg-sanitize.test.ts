// @vitest-environment jsdom
/** SVG 消毒（§7.7）：只在这一个测试文件里要 DOM，所以按文件开 jsdom，
 *  别的测试继续跑 node（快得多，也不用全局装环境）。 */

import { describe, expect, it } from "vitest";
import { cleanPrefix, sanitizeSvg } from "@/lib/svg-sanitize";

const WRAP = (inner: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 50">${inner}</svg>`;

describe("可执行面", () => {
  it("script 元素（含嵌套）整段删掉", () => {
    const out = sanitizeSvg(
      WRAP("<script>alert(1)</script><g><script>alert(2)</script></g>"),
      "s1"
    );
    expect(out).not.toContain("script");
    expect(out).not.toContain("alert");
  });

  it("on* 事件属性删掉，别的属性留着", () => {
    const out = sanitizeSvg(WRAP('<rect onclick="x()" onload="y()" fill="red" />'), "s1");
    expect(out).not.toContain("onclick");
    expect(out).not.toContain("onload");
    expect(out).toContain('fill="red"');
  });

  it("javascript: / data:text 链接删属性，http(s) 与 #锚点留着", () => {
    const out = sanitizeSvg(
      WRAP(
        '<a href="javascript:alert(1)"><text>a</text></a>' +
          '<image href="data:image/svg+xml;base64,PHN2Zz4=" />' +
          '<image href="data:image/png;base64,AAAA" />' +
          '<a href="https://example.com"><text>b</text></a>'
      ),
      "s1"
    );
    expect(out).not.toContain("javascript:");
    expect(out).not.toContain("image/svg+xml");
    expect(out).toContain("data:image/png;base64,AAAA");
    expect(out).toContain("https://example.com");
  });

  it("foreignObject / iframe / style / link 删掉", () => {
    const out = sanitizeSvg(
      WRAP(
        '<foreignObject><body>x</body></foreignObject><iframe src="https://e.com"></iframe>' +
          '<style>*{display:none}</style><link href="https://e.com/a.css" />'
      ),
      "s1"
    );
    for (const bad of ["foreignObject", "iframe", "display:none", "<link"]) {
      expect(out).not.toContain(bad);
    }
  });
});

describe("id 前缀", () => {
  it("id 加前缀，marker-end 引用同步改写（多图共存不串号）", () => {
    const out = sanitizeSvg(
      WRAP(
        '<defs><marker id="arrow"><path d="M0 0L1 1" /></marker></defs>' +
          '<path class="arr" d="M0 0L5 5" marker-end="url(#arrow)" />'
      ),
      "s7"
    );
    expect(out).toContain('id="s7-arrow"');
    expect(out).toContain("url(#s7-arrow)");
    expect(out).not.toContain("url(#arrow)");
  });

  it("href=\"#id\" 形式的引用也改写，带引号的 url('#id') 也认", () => {
    const out = sanitizeSvg(
      WRAP(
        '<clipPath id="c1"><rect width="1" height="1" /></clipPath>' +
          '<rect clip-path="url(\'#c1\')" /><use href="#c1" />'
      ),
      "s2"
    );
    expect(out).toContain('id="s2-c1"');
    expect(out).toContain('href="#s2-c1"');
    expect(out).not.toContain('href="#c1"');
  });

  it("重复 id 都指向第一次的前缀名（引用不会被后一个偷走）", () => {
    const out = sanitizeSvg(WRAP('<g id="a"></g><g id="a"></g>'), "s3");
    expect(out.match(/id="s3-a"/g)).toHaveLength(2);
  });

  it("没有 id 的图原样返回（不白加前缀）", () => {
    const out = sanitizeSvg(WRAP('<circle cx="5" cy="5" r="1" />'), "s4");
    expect(out).toContain("<circle");
    expect(out).not.toContain("s4");
  });
});

describe("非法输入", () => {
  it("根元素不是 svg 就抛（错误卡由调用方渲染）", () => {
    expect(() => sanitizeSvg("<div>不是图</div>", "s1")).toThrow("根元素不是");
  });

  it("解析不了的 XML 抛", () => {
    expect(() => sanitizeSvg('<svg viewBox="0 0 1 1"><unclosed>', "s1")).toThrow();
  });

  it("cleanPrefix 把 useId 里的冒号之类换成短横，且不以短横开头（id 得是合法 XML 名）", () => {
    expect(cleanPrefix(":r0:")).toBe("s-r0-");
    expect(cleanPrefix("")).toBe("s");
    expect(cleanPrefix("a b")).toBe("a-b");
    expect(cleanPrefix("fine-id")).toBe("fine-id");
  });
});
