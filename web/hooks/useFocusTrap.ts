"use client";

/**
 * 焦点圈闭（窄屏抽屉专用）：active 期间把焦点关进容器里，Tab / Shift+Tab 在容器内循环，
 * 关闭时把焦点还给打开它的那个元素。
 *
 * 只在窄屏抽屉态调用（调用方自己乘上 `useIsNarrow`）——桌面端侧栏是常驻的静态面板，
 * 圈住焦点反而走不出去。手写不引库：行为就这三件事，第三方库带进来的是配置面。
 */

import { useEffect, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** 可见才可聚焦：抽屉是 translate 挪出去的，收起时元素还在 DOM 里。 */
function focusablesIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.offsetParent !== null || el === document.activeElement
  );
}

export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    const root = ref.current;
    if (!active || !root) {
      return;
    }
    const restoreTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    focusablesIn(root)[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") {
        return;
      }
      const items = focusablesIn(root);
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const index = items.indexOf(document.activeElement as HTMLElement);
      const edge = event.shiftKey ? index <= 0 : index === -1 || index === items.length - 1;
      if (edge) {
        event.preventDefault();
        (event.shiftKey ? items[items.length - 1] : items[0]).focus();
      }
    };

    root.addEventListener("keydown", onKeyDown);
    return () => {
      root.removeEventListener("keydown", onKeyDown);
      restoreTo?.focus(); // 关抽屉后焦点回触发器，不然会掉到 body 上
    };
  }, [ref, active]);
}
