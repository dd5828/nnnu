/** 消息列表的纯函数（单测见 tests/chat-messages.test.ts）。 */

/** 「重新生成」的乐观切片：裁到末条 user 为止（含），后面全部切掉。
 *
 * 与后端语义对齐（§6.8：删末条 user 之后的全部 assistant、复用末条 user 重跑）。
 * 没有 user 消息、或末尾本来就是 user（没有旧回复可删）时返回 null = 不切片。
 */
export function messagesThroughLastUser<T extends { role: string }>(messages: T[]): T[] | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") {
      return i === messages.length - 1 ? null : messages.slice(0, i + 1);
    }
  }
  return null;
}

interface MergeableMessage {
  id: string;
  role: string;
  content: string;
  thinking: string | null;
  tool_calls: readonly unknown[];
  citations: readonly unknown[];
  cost: { tokens: number; cost: number } | null;
  created_at: number;
}

/** 终局重取时按 id 复用旧对象引用：字段全等就沿用 prev 的那一份。
 *
 * 这不是省内存，是省渲染：历史消息对象引用一变，memo 过的 AssistantMessage /
 * Markdown 全部重渲（长会话每条都要重新 parse 一遍 Markdown）。带工具调用/引用
 * 的消息服务器每次都是新数组，比不动就重建（这类消息少，代价可以接受）；
 * 其余（占绝大多数的纯文本消息）内容没变就沿用旧引用，整列表原地不动。
 */
export function mergeMessages<T extends MergeableMessage>(prev: T[], next: T[]): T[] {
  const byId = new Map(prev.map((message) => [message.id, message]));
  return next.map((message) => {
    const old = byId.get(message.id);
    if (old && sameMessage(old, message)) {
      return old;
    }
    return message;
  });
}

function sameMessage(a: MergeableMessage, b: MergeableMessage): boolean {
  return (
    a.role === b.role &&
    a.content === b.content &&
    a.thinking === b.thinking &&
    a.created_at === b.created_at &&
    a.tool_calls.length === 0 &&
    b.tool_calls.length === 0 &&
    a.citations.length === 0 &&
    b.citations.length === 0 &&
    (a.cost?.tokens ?? null) === (b.cost?.tokens ?? null) &&
    (a.cost?.cost ?? null) === (b.cost?.cost ?? null)
  );
}
