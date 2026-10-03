import { describe, expect, it } from "vitest";
import { mergeMessages, messagesThroughLastUser } from "@/lib/chat-messages";

interface Msg {
  id: string;
  role: string;
  content: string;
  thinking: string | null;
  tool_calls: readonly unknown[];
  citations: readonly unknown[];
  cost: { tokens: number; cost: number } | null;
  created_at: number;
}

function msg(role: string, content: string, id = role + content): Msg {
  return {
    id,
    role,
    content,
    thinking: null,
    tool_calls: [],
    citations: [],
    cost: null,
    created_at: 1,
  };
}

describe("重新生成的乐观切片", () => {
  it("裁到末条 user 为止，后面的 assistant 全切掉", () => {
    const list = [
      msg("user", "问1"),
      msg("assistant", "答1"),
      msg("user", "问2"),
      msg("assistant", "答2"),
    ];
    expect(messagesThroughLastUser(list)?.map((m) => m.content)).toEqual(["问1", "答1", "问2"]);
  });

  it("末尾本来就是 user（没有旧回复）：不切片", () => {
    expect(messagesThroughLastUser([msg("user", "问1")])).toBeNull();
    expect(messagesThroughLastUser([msg("user", "问1"), msg("user", "问2")])).toBeNull();
  });

  it("没有 user 消息：不切片", () => {
    expect(messagesThroughLastUser([msg("assistant", "答1")])).toBeNull();
    expect(messagesThroughLastUser([])).toBeNull();
  });
});

describe("终局重取的消息合并", () => {
  it("内容没变的旧消息沿用原对象引用（memo 才挡得住重渲）", () => {
    const prev = [msg("user", "问1", "u1"), msg("assistant", "答1", "a1")];
    const next = [
      msg("user", "问1", "u1"),
      msg("assistant", "答1", "a1"),
      msg("assistant", "新", "a2"),
    ];
    const merged = mergeMessages(prev, next);
    expect(merged[0]).toBe(prev[0]);
    expect(merged[1]).toBe(prev[1]);
    expect(merged[2]).toEqual(next[2]);
  });

  it("内容变了（服务器订正）：换成新对象", () => {
    const prev = [msg("assistant", "半截", "a1")];
    const next = [msg("assistant", "完整版", "a1")];
    expect(mergeMessages(prev, next)[0]).toBe(next[0]);
  });

  it("带工具调用/引用的消息一律重建（服务器每次都是新数组，比不出等价）", () => {
    const prev = [msg("assistant", "答", "a1")];
    const next = [{ ...msg("assistant", "答", "a1"), tool_calls: [{ name: "t" }] }];
    expect(mergeMessages(prev, next)[0]).toBe(next[0]);
    const cited = [{ ...msg("assistant", "答", "a1"), citations: [{ title: "来源" }] }];
    expect(mergeMessages(prev, cited)[0]).toBe(cited[0]);
  });

  it("成本数值一致就等价（服务器每次给新的 cost 对象）", () => {
    const prev = [{ ...msg("assistant", "答", "a1"), cost: { tokens: 10, cost: 0.1 } }];
    const next = [{ ...msg("assistant", "答", "a1"), cost: { tokens: 10, cost: 0.1 } }];
    expect(mergeMessages(prev, next)[0]).toBe(prev[0]);
    const changed = [{ ...msg("assistant", "答", "a1"), cost: { tokens: 11, cost: 0.1 } }];
    expect(mergeMessages(prev, changed)[0]).toBe(changed[0]);
  });

  it("换会话：id 全不同，全部取新", () => {
    const prev = [msg("user", "旧会话", "u1")];
    const next = [msg("user", "新会话", "u9")];
    expect(mergeMessages(prev, next)[0]).toBe(next[0]);
  });
});
