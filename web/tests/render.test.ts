/** 渲染围栏解析（§7.7/§7.8 正文契约）：纯函数，node 环境直接跑。
 *
 * 与后端 `src/nnnu/services/render/validation.py` 是一对：后端拼正文时用围栏契约，
 * 前端拆正文时用这套函数；哪边改了记得两边一起改。
 */

import { describe, expect, it } from "vitest";
import {
  ARTIFACT_LANG,
  RENDER_FILE_EXT,
  fences,
  firstRenderBlock,
  isArtifactLang,
  isRenderLang,
  kindFromLang,
  lastRenderBlock,
  parseArtifact,
  parseArtifactJson,
  renderBlocks,
  toFenced,
} from "@/lib/render";

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>';

describe("围栏扫描", () => {
  it("扫出普通围栏，语言标记小写化", () => {
    expect(fences("前言\n```SVG\nabc\n```\n后记")).toEqual([{ lang: "svg", code: "abc" }]);
  });

  it("多个围栏按出现顺序", () => {
    const text = "```svg\nA\n```\n中间\n```python\nB\n```";
    expect(fences(text)).toEqual([
      { lang: "svg", code: "A" },
      { lang: "python", code: "B" },
    ]);
  });

  it("未闭合的最后一个围栏也收（流式正文会短暂停在这里）", () => {
    expect(fences("```svg\nA\nB")).toEqual([{ lang: "svg", code: "A\nB" }]);
  });

  it("围栏体保留内部空行", () => {
    expect(fences("```text\na\n\nb\n```")[0].code).toBe("a\n\nb");
  });

  it("没有语言标记的围栏是空语言", () => {
    expect(fences("```\nplain\n```")).toEqual([{ lang: "", code: "plain" }]);
  });
});

describe("渲染块", () => {
  it("只认四种渲染语言，别的围栏不进（哪怕内容像 JSON）", () => {
    const text = '```json\n{"series": []}\n```\n```echarts\n{"series": []}\n```';
    expect(renderBlocks(text)).toEqual([{ kind: "echarts", code: '{"series": []}' }]);
  });

  it("围栏体首尾空白被清掉", () => {
    expect(renderBlocks(`\`\`\`svg\n\n${SVG}\n\n\`\`\``)).toEqual([{ kind: "svg", code: SVG }]);
  });

  it("firstRenderBlock 拿第一个，lastRenderBlock 拿最后一个", () => {
    const text = `\`\`\`mermaid\nflowchart TD\n\`\`\`\n\`\`\`svg\n${SVG}\n\`\`\``;
    expect(firstRenderBlock(text)?.kind).toBe("mermaid");
    expect(lastRenderBlock(text)?.kind).toBe("svg");
  });

  it("拆不出来就是 null（调用方照常走 Markdown 兜底）", () => {
    expect(firstRenderBlock("## 标题\n\n纯文字")).toBeNull();
    expect(lastRenderBlock("")).toBeNull();
  });

  it("语言判定与文件后缀", () => {
    expect(kindFromLang("svg")).toBe("svg");
    expect(kindFromLang("json")).toBeNull();
    expect(isRenderLang("mermaid")).toBe(true);
    expect(isRenderLang("python")).toBe(false);
    expect(isArtifactLang(ARTIFACT_LANG)).toBe(true);
    expect(RENDER_FILE_EXT).toEqual({ svg: "svg", echarts: "json", mermaid: "mmd", html: "html" });
  });
});

describe("产物围栏", () => {
  const payload = {
    url: "/api/v1/renders/rnd-abc12345",
    filename: "animation.mp4",
    mime: "video/mp4",
    kind: "video",
    attempts: 2,
  };

  it("从正文里解析出产物信息", () => {
    const text = `## 标题\n\n总结\n\n\`\`\`${ARTIFACT_LANG}\n${JSON.stringify(payload)}\n\`\`\``;
    expect(parseArtifact(text)).toEqual(payload);
  });

  it("缺 url / 坏 JSON / 根本没有 → null", () => {
    expect(parseArtifactJson('{"filename": "a.mp4"}')).toBeNull();
    expect(parseArtifactJson("{不是 JSON")).toBeNull();
    expect(parseArtifactJson('["数组不行"]')).toBeNull();
    expect(parseArtifact("## 无产物\n\n```python\nprint(1)\n```")).toBeNull();
  });

  it("字段缺失时给兜底值（url 在就算有效）", () => {
    expect(parseArtifactJson('{"url": "/api/v1/renders/rnd-abc12345"}')).toEqual({
      url: "/api/v1/renders/rnd-abc12345",
      filename: "",
      mime: "",
      kind: "",
      attempts: 0,
    });
  });

  it("toFenced 还原成能再解析的围栏（存笔记本用）", () => {
    const fenced = toFenced("svg", SVG);
    expect(fenced).toBe(`\`\`\`svg\n${SVG}\n\`\`\``);
    expect(firstRenderBlock(fenced)).toEqual({ kind: "svg", code: SVG });
  });
});
