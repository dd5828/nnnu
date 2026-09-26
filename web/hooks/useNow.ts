"use client";

import { useSyncExternalStore } from "react";

/** 挂载时刻的秒级时间戳（服务端/hydration 快照给 0 = epoch）。
 *
 *  渲染里直接 `Date.now()` 会被 react-hooks/purity 拦下（渲染必须纯），在 effect 里
 *  setState 又踩 set-state-in-effect，所以走 useSyncExternalStore（照 useMounted 的写法）：
 *  subscribe 在挂载时取一次时间并缓存——快照值保持稳定，不会引发重渲染循环。
 *
 *  看板的「还剩几天复习」不需要秒级跳动，挂载取一次就够；首帧用 0（epoch）会让
 *  复习条目先按「未来」渲染一帧，红色由后端的 overdue 给，不会闪错。 */

let mountedAt = 0;

const subscribe = () => {
  mountedAt = Date.now() / 1000;
  return () => {};
};

export function useNow(): number {
  return useSyncExternalStore(
    subscribe,
    () => mountedAt,
    () => 0
  );
}
