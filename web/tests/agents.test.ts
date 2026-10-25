/** 子代理界面的纯函数（§7.12）：活动流摘要行的参数摊平。 */

import { describe, expect, it } from "vitest";

import { argsLine } from "@/lib/agents";

describe("argsLine", () => {
  it("键值对摊平成一行，字符串原样、其余走 JSON", () => {
    expect(argsLine({ path: "src/a.py", timeout: 30, force: true })).toBe(
      "path=src/a.py timeout=30 force=true"
    );
  });

  it("值里的换行与缩进折成空格（摘要行不能变成多行文本）", () => {
    expect(argsLine({ content: "line1\n  line2\t" })).toBe("content=line1 line2");
  });

  it("没有参数/空对象给空串（调用方据此决定要不要留那个空格）", () => {
    expect(argsLine(null)).toBe("");
    expect(argsLine({})).toBe("");
  });
});
