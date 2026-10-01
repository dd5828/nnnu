"use client";

/** 窄屏（< md，Tailwind 默认 768px）判定：抽屉的「覆盖态」行为只在这个宽度下生效。 */

import { useEffect, useState } from "react";

const QUERY = "(min-width: 768px)"; // 与 md: 前缀同一条线，改了这里也要改 Tailwind 那边的用法

/** 只在浏览器里给初值；SSR 下先按桌面算，挂载后立刻校正（抽屉本来就是客户端交互物）。 */
function readIsNarrow(): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  return !window.matchMedia(QUERY).matches;
}

export function useIsNarrow(): boolean {
  const [narrow, setNarrow] = useState(readIsNarrow);

  useEffect(() => {
    const media = window.matchMedia(QUERY);
    const sync = () => setNarrow(!media.matches);
    sync(); // 首帧在 SSR 值上渲染过，挂载时校正一次
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  return narrow;
}
