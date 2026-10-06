/** 对话引用的编解码（§7.1）：待发 → wire 载荷、待发 → 乐观快照、服务器 metadata 宽容解析。 */

import { describe, expect, it } from "vitest";

import {
  normalizeRefs,
  questionRefLabel,
  refKey,
  toSnapshotEntries,
  toWireRefs,
  type PendingRef,
} from "@/lib/refs";

const recordRef: PendingRef = {
  kind: "notebook_record",
  notebook_id: "nb-1",
  record_id: "nbr-1",
  label: "速记 · 极限",
};
const questionRef: PendingRef = { kind: "question", question_id: "q-1", label: "下列极限…" };

describe("toWireRefs", () => {
  it("按类分组，只带后端认的 id 字段（label 是本地展示用，不发）", () => {
    expect(toWireRefs([recordRef, questionRef])).toEqual({
      notebooks: [{ notebook_id: "nb-1", record_id: "nbr-1" }],
      questions: [{ question_id: "q-1" }],
    });
  });

  it("空列表给空组：全量下发语义（不选引用也说得清楚）", () => {
    expect(toWireRefs([])).toEqual({ notebooks: [], questions: [] });
  });
});

describe("refKey", () => {
  it("同类同 id 同键、异类不同键（去重与选中判断共用）", () => {
    expect(refKey(recordRef)).toBe(refKey({ ...recordRef }));
    expect(refKey(recordRef)).not.toBe(refKey(questionRef));
  });
});

describe("toSnapshotEntries", () => {
  it("本地选的引用一律标已解析（乐观消息的 metadata 用）", () => {
    expect(toSnapshotEntries([recordRef, questionRef])).toEqual([
      {
        kind: "notebook_record",
        notebook_id: "nb-1",
        record_id: "nbr-1",
        label: "速记 · 极限",
        resolved: true,
      },
      { kind: "question", question_id: "q-1", label: "下列极限…", resolved: true },
    ]);
  });
});

describe("normalizeRefs（服务器 metadata 宽容解析）", () => {
  it("坏条目跳过，好条目原样收（对齐后端 _refs_from_metadata）", () => {
    const raw = {
      refs: [
        {
          kind: "notebook_record",
          notebook_id: "nb-1",
          record_id: "nbr-1",
          label: "a",
          resolved: true,
        },
        { kind: "question", label: "缺 id" },
        null,
        42,
        { kind: "question", question_id: "q-1", label: "b", resolved: false },
      ],
    };
    expect(normalizeRefs(raw)).toEqual([
      {
        kind: "notebook_record",
        notebook_id: "nb-1",
        record_id: "nbr-1",
        label: "a",
        resolved: true,
      },
      { kind: "question", question_id: "q-1", label: "b", resolved: false },
    ]);
  });

  it("已删条目的空 label / 缺 notebook_id 兜住（后端快照就是空 label）", () => {
    expect(
      normalizeRefs({ refs: [{ kind: "notebook_record", record_id: "nbr-9", resolved: false }] })
    ).toEqual([
      { kind: "notebook_record", notebook_id: "", record_id: "nbr-9", label: "", resolved: false },
    ]);
  });

  it("null / 无 refs / 非数组一律空（老消息、手改库都不炸）", () => {
    expect(normalizeRefs(null)).toEqual([]);
    expect(normalizeRefs(undefined)).toEqual([]);
    expect(normalizeRefs({})).toEqual([]);
    expect(normalizeRefs({ refs: "x" })).toEqual([]);
  });
});

describe("questionRefLabel", () => {
  it("折空白成单行、超 60 字截断加省略号", () => {
    expect(questionRefLabel(" 求\n极限 \t x ")).toBe("求 极限 x");
    const label = questionRefLabel("题".repeat(80));
    expect(label.length).toBe(61); // 60 + 省略号
    expect(label.endsWith("…")).toBe(true);
  });
});
