import { describe, expect, it } from "vitest";
import {
  BLOCK_STATUSES,
  BLOCK_TYPES,
  BOOK_POLL_MS,
  BOOK_STATUSES,
  blockStatusClass,
  blockStatusKey,
  blockTypeKey,
  bookStatusClass,
  bookStatusKey,
  calloutVariant,
  chapterRows,
  compileStats,
  deepDiveSources,
  driftChapters,
  formatCost,
  formatTokens,
  hasPendingBlocks,
  isBookBusy,
  markdownOf,
  moveChapter,
  pageOfChapter,
  parseCode,
  parseFlashcards,
  parseQuiz,
  parseTimeline,
  percentOf,
  planTypeCounts,
  removeChapter,
  renameChapter,
  sourceSummary,
  sourceRefText,
  splitDuration,
} from "@/lib/book";
import type { BookChapter, BookPageMeta, BookSpine } from "@/types/api";

function chapter(key: string, title = key): BookChapter {
  return {
    key,
    title,
    summary: "",
    objectives: [],
    content_type: "theory",
    blocks_plan: [],
    source_refs: [],
  };
}

function page(
  chapterKey: string,
  statuses: ("pending" | "compiling" | "done" | "error")[],
  pageNo = 1
): BookPageMeta {
  return {
    id: `bp-${chapterKey}`,
    chapter_key: chapterKey,
    page_no: pageNo,
    visited: false,
    bookmarked: false,
    block_count: statuses.length,
    blocks_done: statuses.filter((status) => status === "done").length,
    blocks_error: statuses.filter((status) => status === "error").length,
    blocks: statuses.map((status, index) => ({
      id: `blk-${index}`,
      type: "text",
      status,
    })),
  };
}

describe("枚举对齐后端", () => {
  it("书状态五档与 book/models.BOOK_STATUSES 同序", () => {
    expect(BOOK_STATUSES).toEqual(["draft", "compiling", "paused", "ready", "error"]);
    expect(new Set(BOOK_STATUSES).size).toBe(BOOK_STATUSES.length);
  });

  it("块状态四档与 BLOCK_STATUSES 同序", () => {
    expect(BLOCK_STATUSES).toEqual(["pending", "compiling", "done", "error"]);
  });

  it("十二类块与 BLOCK_TYPES 同序（换类型菜单顺序即此）", () => {
    expect(BLOCK_TYPES).toEqual([
      "text",
      "callout",
      "quiz",
      "flashcard",
      "timeline",
      "code",
      "figure",
      "interactive_html",
      "animation",
      "concept_graph",
      "deep_dive",
      "note",
    ]);
    expect(new Set(BLOCK_TYPES).size).toBe(BLOCK_TYPES.length);
  });

  it("只有 compiling 轮询", () => {
    expect(isBookBusy("compiling")).toBe(true);
    for (const status of ["draft", "paused", "ready", "error"] as const) {
      expect(isBookBusy(status)).toBe(false);
    }
    expect(BOOK_POLL_MS).toBe(1500);
  });

  it("文案键与配色类目齐全", () => {
    expect(bookStatusKey("paused")).toBe("book.status_paused");
    expect(bookStatusClass("ready")).toContain("success");
    expect(blockStatusKey("compiling")).toBe("book.blockStatus_compiling");
    expect(blockStatusClass("error")).toContain("danger");
    expect(blockTypeKey("interactive_html")).toBe("book.blockType_interactive_html");
  });
});

describe("编译看板读数", () => {
  it("percentOf 除零给 0，其余四舍五入", () => {
    expect(percentOf(0, 0)).toBe(0);
    expect(percentOf(1, 3)).toBe(33);
    expect(percentOf(3, 3)).toBe(100);
  });

  it("compileStats 摊平所有页的块逐一计数", () => {
    const stats = compileStats([
      page("ch-1", ["done", "done", "compiling"]),
      page("ch-2", ["pending", "error"]),
    ]);
    expect(stats).toEqual({
      total: 5,
      done: 2,
      error: 1,
      running: 1,
      pending: 1,
      percent: 40,
    });
  });

  it("没有页/没有块时不炸", () => {
    expect(compileStats([]).percent).toBe(0);
    expect(compileStats([page("ch-1", [])]).total).toBe(0);
  });
});

describe("目录（章 × 页）", () => {
  const spine: BookSpine = {
    chapters: [chapter("ch-1", "第一章"), chapter("ch-2", "第二章")],
    concept_graph: { nodes: [], edges: [] },
    language: "zh",
    material_chars: 0,
  };

  it("chapterRows 按 chapter_key 配页；配不上给 null", () => {
    const rows = chapterRows(spine, [page("ch-2", ["done"])]);
    expect(rows.map((row) => row.chapter.key)).toEqual(["ch-1", "ch-2"]);
    expect(rows[0].page).toBeNull();
    expect(rows[1].page?.chapter_key).toBe("ch-2");
  });

  it("pageOfChapter 找章第一页", () => {
    const pages = [page("ch-1", ["done"], 1), page("ch-1", ["done"], 2), page("ch-2", ["done"])];
    expect(pageOfChapter(pages, "ch-1")?.page_no).toBe(1);
    expect(pageOfChapter(pages, "ch-9")).toBeNull();
  });

  it("driftChapters：章引到的源命中漂移才算受影响", () => {
    const withRefs: BookSpine = {
      ...spine,
      chapters: [
        {
          ...chapter("ch-1", "第一章"),
          source_refs: [{ kind: "kb", ref: "kbdoc-1", label: "甲" }],
        },
        {
          ...chapter("ch-2", "第二章"),
          source_refs: [{ kind: "kb", ref: "kbdoc-2", label: "乙" }],
        },
        { ...chapter("ch-3", "第三章"), source_refs: [] },
      ],
    };
    const drift = [
      { change: "changed" as const, ref: "kbdoc-1", label: "甲" },
      { change: "added" as const, ref: "kbdoc-9", label: "没被引用" },
    ];
    expect(driftChapters(withRefs, drift).map((item) => item.key)).toEqual(["ch-1"]);
    expect(driftChapters(withRefs, [])).toEqual([]);
  });
});

describe("目录编辑", () => {
  const chapters = [chapter("ch-1"), chapter("ch-2"), chapter("ch-3")];

  it("moveChapter 相邻交换，到头原地返回", () => {
    expect(moveChapter(chapters, 1, "up").map((item) => item.key)).toEqual([
      "ch-2",
      "ch-1",
      "ch-3",
    ]);
    expect(moveChapter(chapters, 1, "down").map((item) => item.key)).toEqual([
      "ch-1",
      "ch-3",
      "ch-2",
    ]);
    expect(moveChapter(chapters, 0, "up")).toBe(chapters);
    expect(moveChapter(chapters, 2, "down")).toBe(chapters);
  });

  it("removeChapter / renameChapter 不动原数组", () => {
    const removed = removeChapter(chapters, "ch-2");
    expect(removed.map((item) => item.key)).toEqual(["ch-1", "ch-3"]);
    const renamed = renameChapter(chapters, "ch-2", "新名字");
    expect(renamed[1].title).toBe("新名字");
    expect(chapters[1].title).toBe("ch-2");
  });

  it("planTypeCounts 汇总块计划（章节行后缀用）", () => {
    const counts = planTypeCounts([
      { type: "text", focus: "开场" },
      { type: "quiz", focus: "小测" },
      { type: "text", focus: "小结" },
    ] as BookChapter["blocks_plan"]);
    expect(counts.get("text")).toBe(2);
    expect(counts.get("quiz")).toBe(1);
  });
});

describe("素材摘要", () => {
  it("没选/缺字段按 0", () => {
    expect(sourceSummary(null).kbs).toBe(0);
    expect(sourceSummary({ kbs: ["a"], notebooks: [], sessions: [], questions: null })).toEqual({
      kbs: 1,
      notebooks: 0,
      sessions: 0,
      questions: "none",
      questionIds: 0,
    });
  });

  it("题库口径直传（all/wrong/ids）", () => {
    const summary = sourceSummary({
      kbs: [],
      notebooks: ["nb-1", "nb-2"],
      sessions: [],
      questions: { filter: "ids", ids: ["q-1", "q-2", "q-3"] },
    });
    expect(summary).toEqual({
      kbs: 0,
      notebooks: 2,
      sessions: 0,
      questions: "ids",
      questionIds: 3,
    });
  });

  it("来源引用标签优先 label，退回 ref", () => {
    expect(sourceRefText({ kind: "kb", ref: "kbdoc-1", label: "教材·第一章.md" })).toBe(
      "教材·第一章.md"
    );
    expect(sourceRefText({ kind: "kb", ref: "kbdoc-1", label: "" })).toBe("kbdoc-1");
  });
});

describe("块 payload 解析", () => {
  it("parseQuiz 收下发过的选项、丢掉坏行，且不带答案", () => {
    const view = parseQuiz({
      stem: "横轴是什么？",
      options: [
        { key: "a", text: "频率" },
        { key: "", text: "没键" },
        { key: "B", text: "" },
        { key: "C", text: "时间" },
      ],
      answer_key: ["A"],
      explanation: "横轴是频率。",
    });
    expect(view.stem).toBe("横轴是什么？");
    expect(view.options).toEqual([
      { key: "A", text: "频率" },
      { key: "C", text: "时间" },
    ]);
    expect(view.explanation).toBe("横轴是频率。");
    expect(JSON.stringify(view)).not.toContain("answer_key"); // 前端视图不携带答案
    expect(parseQuiz({}).options).toEqual([]);
  });

  it("parseFlashcards / parseTimeline 坏行丢弃", () => {
    expect(
      parseFlashcards({
        cards: [{ front: "傅里叶变换做什么？", back: "分解成频率成分" }, { front: "缺背" }],
      }).cards
    ).toEqual([{ front: "傅里叶变换做什么？", back: "分解成频率成分" }]);
    expect(
      parseTimeline({
        events: [
          { when: "1822", title: "发表热传导理论", detail: "引入三角级数" },
          { title: "没年份" },
        ],
      }).events
    ).toEqual([{ when: "1822", title: "发表热传导理论", detail: "引入三角级数" }]);
    expect(parseFlashcards({ cards: "不是数组" }).cards).toEqual([]);
  });

  it("parseCode 语言缺省 text；markdownOf / calloutVariant / deepDiveSources 宽容取值", () => {
    expect(parseCode({ code: "print(1)" })).toEqual({
      language: "text",
      code: "print(1)",
      explanation: "",
    });
    expect(markdownOf({ markdown: "正文" })).toBe("正文");
    expect(markdownOf({})).toBe("");
    expect(calloutVariant({ variant: "warn" })).toBe("warn");
    expect(calloutVariant({ variant: "乱写" })).toBe("info");
    expect(calloutVariant({})).toBe("info");
    expect(deepDiveSources({ sources: ["甲", 3, "乙"] })).toEqual(["甲", "乙"]);
    expect(deepDiveSources({})).toEqual([]);
  });

  it("hasPendingBlocks：done/error 之外都算没落定（页轮询开关）", () => {
    const block = (status: "pending" | "compiling" | "done" | "error") => ({ status });
    expect(hasPendingBlocks([block("done"), block("error")])).toBe(false);
    expect(hasPendingBlocks([block("done"), block("pending")])).toBe(true);
    expect(hasPendingBlocks([block("compiling")])).toBe(true);
    expect(hasPendingBlocks([])).toBe(false);
  });
});

describe("数字格式化", () => {
  it("token：千位进 k，万位取整", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(1234)).toBe("1.2k");
    expect(formatTokens(123_456)).toBe("123k");
  });

  it("费用：小于一分给三位，其余两位", () => {
    expect(formatCost(0)).toBe("$0");
    expect(formatCost(0.0031)).toBe("$0.003");
    expect(formatCost(1.2345)).toBe("$1.23");
  });

  it("秒数拆成分/秒（负数/NaN 归零）", () => {
    expect(splitDuration(200)).toEqual({ minutes: 3, seconds: 20 });
    expect(splitDuration(-5)).toEqual({ minutes: 0, seconds: 0 });
    expect(splitDuration(Number.NaN)).toEqual({ minutes: 0, seconds: 0 });
  });
});
