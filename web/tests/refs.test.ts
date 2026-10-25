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
const agentRef: PendingRef = {
  kind: "agent_session",
  agent_session_id: "imp-1a2b3c4d",
  label: "昨天问过什么",
};

describe("toWireRefs", () => {
  it("按类分组，只带后端认的 id 字段（label 是本地展示用，不发）", () => {
    expect(toWireRefs([recordRef, questionRef, agentRef])).toEqual({
      notebooks: [{ notebook_id: "nb-1", record_id: "nbr-1" }],
      questions: [{ question_id: "q-1" }],
      agent_sessions: ["imp-1a2b3c4d"],
    });
  });

  it("空列表给空组：全量下发语义（不选引用也说得清楚）", () => {
    expect(toWireRefs([])).toEqual({ notebooks: [], questions: [], agent_sessions: [] });
  });
});

describe("refKey", () => {
  it("同类同 id 同键、异类不同键（去重与选中判断共用）", () => {
    expect(refKey(recordRef)).toBe(refKey({ ...recordRef }));
    expect(refKey(recordRef)).not.toBe(refKey(questionRef));
    // 导入会话走自己的键前缀：id 跟记录/题目形态不同，撞不撞都得各归各的（§7.12）
    expect(refKey(agentRef)).toBe("agent_session:imp-1a2b3c4d");
    expect(refKey(agentRef)).not.toBe(refKey(questionRef));
  });
});

describe("toSnapshotEntries", () => {
  it("本地选的引用一律标已解析（乐观消息的 metadata 用）", () => {
    expect(toSnapshotEntries([recordRef, questionRef, agentRef])).toEqual([
      {
        kind: "notebook_record",
        notebook_id: "nb-1",
        record_id: "nbr-1",
        label: "速记 · 极限",
        resolved: true,
      },
      { kind: "question", question_id: "q-1", label: "下列极限…", resolved: true },
      {
        kind: "agent_session",
        agent_session_id: "imp-1a2b3c4d",
        label: "昨天问过什么",
        resolved: true,
      },
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

  it("导入会话条目照收，缺 id 的照样跳过（§7.12）", () => {
    const raw = {
      refs: [
        {
          kind: "agent_session",
          agent_session_id: "imp-1a2b3c4d",
          label: "昨天问过什么",
          resolved: true,
        },
        { kind: "agent_session", label: "缺 id" },
        { kind: "session", session_id: "s-1", label: "本应用的历史会话", resolved: true },
      ],
    };
    // `session`（本应用自己的历史会话）后端会写、前端不认：跳过而不是崩
    expect(normalizeRefs(raw)).toEqual([
      {
        kind: "agent_session",
        agent_session_id: "imp-1a2b3c4d",
        label: "昨天问过什么",
        resolved: true,
      },
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
