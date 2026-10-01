import { describe, expect, it } from "vitest";
import {
  ACTIVE_LINE_PX,
  JUMP_TOP_GAP_PX,
  TRACK_PAD_PX,
  activeIndexFromTops,
  buildQuestionEntries,
  isRailQuestion,
  jumpTargetTop,
  plainTextPreview,
  previewTopPx,
  rowHeightPx,
  shouldShowRail,
} from "@/lib/chat-rail";
import type { RailMessage } from "@/lib/chat-rail";

/** 拼一条够用的消息（只看 lib 用到的字段）。 */
function msg(role: RailMessage["role"], content: string, id = "m1"): RailMessage {
  return { id, role, content };
}

describe("markdown 扁平化", () => {
  it("围栏代码整段丢，没闭合的从围栏丢到文末", () => {
    expect(plainTextPreview("看这段：\n```py\nprint(1)\n```\n就这样")).toBe("看这段： 就这样");
    expect(plainTextPreview("开始\n```py\nprint(1)\nprint(2)")).toBe("开始");
  });

  it("图片整段丢、链接留字", () => {
    expect(plainTextPreview("![图](https://a/b.png) 对比 [文档](https://a/doc) 的写法")).toBe(
      "对比 文档 的写法"
    );
  });

  it("标题/列表/引用去标记留字", () => {
    expect(plainTextPreview("## 做法\n- 先建索引\n1. 再切流量\n> 注意回滚")).toBe(
      "做法 先建索引 再切流量 注意回滚"
    );
  });

  it("强调与行内 code 去壳：粗体、斜体、删除线、反引号都不留标记", () => {
    expect(plainTextPreview("**重点**是 `config.py` 里的 ~~旧值~~ *别改*")).toBe(
      "重点是 config.py 里的 旧值 别改"
    );
  });

  it("下划线斜体去掉，但 snake_case 不动", () => {
    expect(plainTextPreview("这里 _强调_ 一下")).toBe("这里 强调 一下");
    expect(plainTextPreview("改 user_profile_name 这个字段")).toBe("改 user_profile_name 这个字段");
  });

  it("乘法号不是斜体标记，不误伤", () => {
    expect(plainTextPreview("算 2 * 3 * 4 的值")).toBe("算 2 * 3 * 4 的值");
  });

  it("分隔线丢、空白折叠、首尾清干净", () => {
    expect(plainTextPreview("上一段\n\n---\n\n下一段")).toBe("上一段 下一段");
    expect(plainTextPreview("  多   空白\n\n换行  ")).toBe("多 空白 换行");
  });

  it("超长截断加省略号，不截出半个尾巴空格", () => {
    expect(plainTextPreview("abcdefghijklmnopqrstuvwxyz", 10)).toBe("abcdefghij…");
    expect(plainTextPreview("abcdefghij klmn", 10)).toBe("abcdefghij…");
    expect(plainTextPreview("正好十个字符啦", 7)).toBe("正好十个字符啦");
  });

  it("空串、纯空白、只有代码块都压成空", () => {
    expect(plainTextPreview("")).toBe("");
    expect(plainTextPreview(" \n\t ")).toBe("");
    expect(plainTextPreview("```\ncode\n```")).toBe("");
  });
});

describe("刻度条目派生", () => {
  it("一问一条，reply 挂到该轮第一条有正文的回复上", () => {
    const entries = buildQuestionEntries([
      msg("user", "第一问", "u1"),
      msg("assistant", "", "a1"),
      msg("assistant", "第一答", "a2"),
      msg("user", "第二问", "u2"),
      msg("assistant", "第二答", "a3"),
    ]);
    expect(entries.map((e) => e.id)).toEqual(["u1", "u2"]);
    expect(entries.map((e) => e.ordinal)).toEqual([1, 2]);
    expect(entries[0].reply).toBe("第一答");
    expect(entries[1].reply).toBe("第二答");
  });

  it("这轮还没回复（或者回复全空的）→ reply 是空串", () => {
    expect(buildQuestionEntries([msg("user", "刚发出去", "u1")])[0].reply).toBe("");
    expect(
      buildQuestionEntries([msg("user", "问", "u1"), msg("assistant", "", "a1")])[0].reply
    ).toBe("");
  });

  it("回复只在下一问之前找：下一问之后的回答不算上一问的", () => {
    const entries = buildQuestionEntries([
      msg("user", "第一问", "u1"),
      msg("user", "第二问", "u2"),
      msg("assistant", "第二答", "a2"),
    ]);
    expect(entries[0].reply).toBe("");
    expect(entries[1].reply).toBe("第二答");
  });

  it("扁平化后为空的用户消息不占刻度，其余 ordinal 照旧连续", () => {
    const entries = buildQuestionEntries([
      msg("user", "真问题一", "u1"),
      msg("assistant", "答一", "a1"),
      msg("user", "```\n只有代码\n```", "u2"),
      msg("user", "真问题二", "u3"),
      msg("assistant", "答二", "a2"),
    ]);
    expect(entries.map((e) => e.id)).toEqual(["u1", "u3"]);
    expect(entries.map((e) => e.ordinal)).toEqual([1, 2]);
  });

  it("短确认消息照常占一格（刻度契约：一个气泡 = 一个刻度）", () => {
    const entries = buildQuestionEntries([
      msg("user", "调研一下向量数据库", "u1"),
      msg("assistant", "大纲如下…", "a1"),
      msg("user", "确认，开始研究", "u2"),
      msg("assistant", "报告正文…", "a2"),
    ]);
    expect(entries.map((e) => e.title)).toEqual(["调研一下向量数据库", "确认，开始研究"]);
  });

  it("乐观 id（local-…）原样保留，回合结束后按真 id 重建", () => {
    expect(buildQuestionEntries([msg("user", "问题", "local-1700000000000")])[0].id).toBe(
      "local-1700000000000"
    );
    expect(buildQuestionEntries([msg("user", "问题", "msg_abc")])[0].id).toBe("msg_abc");
  });

  it("标题取扁平化后的文本，代码块不把预览撑爆", () => {
    const entries = buildQuestionEntries([
      msg("user", "帮我看看\n```ts\nconst a = 1\n```\n这段", "u1"),
    ]);
    expect(entries[0].title).toBe("帮我看看 这段");
  });
});

describe("活动刻度", () => {
  it("没有刻度 → -1", () => {
    expect(activeIndexFromTops([])).toBe(-1);
  });

  it("谁都没过线（还在最顶上）→ 第一格", () => {
    expect(activeIndexFromTops([300, 900], ACTIVE_LINE_PX)).toBe(0);
  });

  it("过线取最后一格；正好压在线上算过", () => {
    expect(activeIndexFromTops([-800, -300, 60, 500], ACTIVE_LINE_PX)).toBe(2);
    expect(activeIndexFromTops([-10, ACTIVE_LINE_PX, 200], ACTIVE_LINE_PX)).toBe(1);
  });

  it("NaN/Infinity 不误判，坏数据当没看见", () => {
    expect(activeIndexFromTops([-5, Number.NaN, 300], ACTIVE_LINE_PX)).toBe(0);
    expect(activeIndexFromTops([-5, Number.POSITIVE_INFINITY], ACTIVE_LINE_PX)).toBe(0);
  });
});

describe("跳转与可见性", () => {
  it("跳转落点 = 当前滚动量 + 元素相对位置 − 顶部留白", () => {
    expect(jumpTargetTop(800, 100, 500)).toBe(500 + 700 - JUMP_TOP_GAP_PX);
    expect(jumpTargetTop(800, 100, 500, 0)).toBe(1200);
  });

  it("一问不显示；槽位 44px 是及格线", () => {
    expect(shouldShowRail(1, 200)).toBe(false);
    expect(shouldShowRail(2, 43)).toBe(false);
    expect(shouldShowRail(2, 44)).toBe(true);
  });

  it("刻度高度分四档，边界取上一档的顶格", () => {
    expect(rowHeightPx(2)).toBe(18);
    expect(rowHeightPx(10)).toBe(18);
    expect(rowHeightPx(11)).toBe(12);
    expect(rowHeightPx(24)).toBe(12);
    expect(rowHeightPx(25)).toBe(8);
    expect(rowHeightPx(40)).toBe(8);
    expect(rowHeightPx(41)).toBe(7);
  });

  it("预览卡对在刻度中心上，轨内滚动要减掉滚动量", () => {
    // 5 格 → 每格 18px：第 2 格（下标 1）中心 = 8 + 18 + 9
    expect(previewTopPx(1, 5)).toBe(TRACK_PAD_PX + 18 + 9);
    expect(previewTopPx(1, 5, 10)).toBe(TRACK_PAD_PX + 18 + 9 - 10);
  });

  it("isRailQuestion：用户消息才占格，压平为空的不要", () => {
    expect(isRailQuestion(msg("user", "问一句"))).toBe(true);
    expect(isRailQuestion(msg("assistant", "答一句"))).toBe(false);
    expect(isRailQuestion(msg("user", "```\ncode\n```"))).toBe(false);
  });
});
