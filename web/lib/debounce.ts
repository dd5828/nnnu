/** 防抖工具（web 侧唯一一份，供 Co-Writer 自动保存用）。
 *
 * 语义是**尾部防抖**，与 throttle.ts 的节流相反：窗口内每次调用都重新计时，
 * 安静满 ms 才执行一次。自动保存要的正是这个——打字期间一个请求都不发，
 * 停手 2 秒才 PATCH 一次。
 *
 * 两个出口按键位不同：
 * - flush()：有欠账就立刻执行（切页前、发起改写前必须调——否则模型看到的
 *   是旧正文，前端选区和服务端切片对不上，直接 409）；
 * - cancel()：丢掉欠账不执行（accept 写回前必须调——2 秒前挂起的 PATCH 会拿
 *   旧内容冲掉刚写回的结果）。
 */

export type Debounced<A extends unknown[]> = {
  /** 调用（每次都顺延定时器）；真正执行的是最后一次的参数 */
  call: (...args: A) => void;
  /** 立刻执行挂起的那一次（没有欠账就什么都不做） */
  flush: () => void;
  /** 丢弃挂起的那一次（不执行） */
  cancel: () => void;
};

export function createDebounce<A extends unknown[]>(
  fn: (...args: A) => void,
  ms: number
): Debounced<A> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let pending: A | null = null;

  const clear = () => {
    if (timer !== null) {
      clearTimeout(timer);
    }
    timer = null;
  };

  return {
    call(...args: A) {
      pending = args;
      clear();
      timer = setTimeout(() => {
        timer = null;
        const args2 = pending;
        pending = null;
        if (args2 !== null) {
          fn(...args2);
        }
      }, ms);
    },
    flush() {
      if (pending === null) {
        return;
      }
      clear();
      const args = pending;
      pending = null;
      fn(...args);
    },
    cancel() {
      clear();
      pending = null;
    },
  };
}
