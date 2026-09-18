"use client";

import { useSyncExternalStore } from "react";

const emptySubscribe = () => () => {};

/**
 * 挂载守卫：客户端 store 水合可能与 SSR 渲染不一致（语言/主题等），
 * hydration 期间用默认值渲染，挂载后再切换到真实值。
 * 用 useSyncExternalStore 实现（服务端快照 false、客户端快照 true），
 * 避免 effect 内同步 setState（react-hooks/set-state-in-effect 规则）。
 */
export function useMounted() {
  return useSyncExternalStore(
    emptySubscribe,
    () => true, // 客户端快照
    () => false // 服务端快照（SSR 与 hydration 期间）
  );
}
