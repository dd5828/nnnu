/** 节流工具（web 侧唯一一份，供流式正文渲染用）。
 *
 * 语义是**节流**，不是防抖：首次调用立即执行，窗口内的后续调用只留最新一份、
 * 到点合并执行一次。
 *
 * 这个区别是硬的：尾部防抖在「每隔几十毫秒来一次、间隔短于窗口」的连续增量下
 * **永远不会触发**——每次新增量都把定时器顺延，于是一直不更新，直到流结束才整段
 * 蹦出来（真机踩过：模型逐字输出，聊天区一动不动，回合结束才一次性出现全篇）。
 */

export type Throttled<A extends unknown[]> = {
  /** 调用（窗口内合并）；首次调用立即执行 */
  call: (...args: A) => void;
  /** 丢掉待执行的那一次（组件卸载用） */
  cancel: () => void;
};

/** 流式文本的渲染节流窗口（ms）：正文与思考共用。50ms≈每帧最多一次重排，
 *  比 90ms 跟手，又远低于每 delta 一渲的密度（§7.21 增量渲染）。 */
export const STREAM_THROTTLE_MS = 50;

export function createThrottle<A extends unknown[]>(
  fn: (...args: A) => void,
  ms: number
): Throttled<A> {
  let lastAt = Number.NEGATIVE_INFINITY; // 首次调用必然立即执行
  let timer: ReturnType<typeof setTimeout> | null = null;
  let pending: A | null = null;

  const flush = () => {
    timer = null;
    if (pending === null) {
      return;
    }
    const args = pending;
    pending = null;
    lastAt = Date.now();
    fn(...args);
  };

  return {
    call(...args: A) {
      pending = args;
      if (lastAt + ms - Date.now() <= 0) {
        if (timer !== null) {
          clearTimeout(timer);
        }
        flush();
        return;
      }
      // 已有定时器就让它继续等：到点执行的是 pending 里最新那一份
      timer ??= setTimeout(flush, lastAt + ms - Date.now());
    },
    cancel() {
      if (timer !== null) {
        clearTimeout(timer);
      }
      timer = null;
      pending = null;
    },
  };
}
