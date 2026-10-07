import { describe, expect, it } from "vitest";
import {
  AUTOSAVE_DELAY_MS,
  CO_WRITER_ACTIONS,
  MAX_SELECTION_CHARS,
  actionLabelKey,
  applyEdit,
  codePointIndex,
  codePointLength,
  draftKey,
  parseDraft,
  resolveDraft,
  serializeDraft,
  utf16Index,
} from "@/lib/co-writer";

describe("动作枚举", () => {
  it("六档与后端 models.ACTIONS 对齐（顺序即工具条顺序）", () => {
    expect(CO_WRITER_ACTIONS).toEqual([
      "rewrite",
      "expand",
      "shorten",
      "translate",
      "tone",
      "free",
    ]);
    expect(new Set(CO_WRITER_ACTIONS).size).toBe(CO_WRITER_ACTIONS.length);
  });

  it("键 → locale 文案键", () => {
    expect(actionLabelKey("expand")).toBe("coWriter.action_expand");
  });

  it("常量与后端同值", () => {
    expect(AUTOSAVE_DELAY_MS).toBe(2000);
    expect(MAX_SELECTION_CHARS).toBe(20_000);
  });
});

describe("草稿镜像", () => {
  it("draftKey 按文档隔离", () => {
    expect(draftKey("cw-0a1b2c3d")).toBe("nnnu.co-writer.draft.cw-0a1b2c3d");
  });

  it("序列化后能原样读回", () => {
    const draft = { content: "# 开场白", savedAt: 1234 };
    expect(parseDraft(serializeDraft(draft))).toEqual(draft);
  });

  it("坏 JSON / 没 content / 空值一律当没有草稿", () => {
    expect(parseDraft("{oops")).toBeNull();
    expect(parseDraft('{"savedAt": 1}')).toBeNull();
    expect(parseDraft(null)).toBeNull();
    expect(parseDraft("")).toBeNull();
  });

  it("savedAt 缺失补 0，不因时间戳坏掉整条草稿", () => {
    expect(parseDraft('{"content": "正文"}')).toEqual({ content: "正文", savedAt: 0 });
  });

  it("草稿与服务端一致（或没草稿）用服务端", () => {
    expect(resolveDraft("一样", { content: "一样", savedAt: 1 })).toEqual({
      content: "一样",
      fromDraft: false,
    });
    expect(resolveDraft("服务端", null)).toEqual({ content: "服务端", fromDraft: false });
  });

  it("草稿不同则默认用草稿，并标记提示条", () => {
    expect(resolveDraft("服务端", { content: "没存出去的", savedAt: 1 })).toEqual({
      content: "没存出去的",
      fromDraft: true,
    });
  });
});

describe("码点换算", () => {
  it("纯 BMP 文本下码点下标与 UTF-16 下标一致", () => {
    const text = "北京是中国的首都。";
    for (let i = 0; i <= text.length; i++) {
      expect(codePointIndex(text, i)).toBe(i);
      expect(utf16Index(text, i)).toBe(i);
    }
  });

  it("emoji/生僻字（代理对）在 JS 里占 2 个码元、码点只算 1", () => {
    const text = "🎉开场😀"; // 两个代理对 + 两个 BMP 字
    expect(text.length).toBe(6);
    expect(codePointLength(text)).toBe(4);
    // 下标 2（🎉 之后）在码点里是 1
    expect(codePointIndex(text, 2)).toBe(1);
    expect(codePointIndex(text, 4)).toBe(3);
    expect(utf16Index(text, 1)).toBe(2);
    expect(utf16Index(text, 3)).toBe(4);
  });

  it("越界下标收敛到两端", () => {
    expect(codePointIndex("ab", -5)).toBe(0);
    expect(codePointIndex("ab", 99)).toBe(2);
    expect(utf16Index("ab", 99)).toBe(2);
  });
});

describe("applyEdit 本地写回", () => {
  it("BMP 选区按码点区间替换", () => {
    const content = "前一段。北京是中国的首都。后一段。";
    const start = codePointIndex(content, content.indexOf("北京"));
    const end = start + codePointLength("北京是中国的首都。");
    expect(applyEdit(content, start, end, "北京是中国的首都，也是政治中心。")).toBe(
      "前一段。北京是中国的首都，也是政治中心。后一段。"
    );
  });

  it("选区前后有 emoji 时偏移不漂", () => {
    const content = "🎉开头🎈北京是中国的首都。尾巴🐍";
    const start = codePointIndex(content, content.indexOf("北京"));
    const end = start + codePointLength("北京是中国的首都。");
    const applied = applyEdit(content, start, end, "北京，首都。");
    expect(applied).toBe("🎉开头🎈北京，首都。尾巴🐍");
  });

  it("替换空区间 = 插入", () => {
    expect(applyEdit("甲乙", 1, 1, "丙")).toBe("甲丙乙");
  });
});
