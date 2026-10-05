"use client";

/** 统一确认弹窗的 Promise 接口（替代 window.confirm）。
 *
 * 调用方 `await confirmDialog({ message: t(...) })`，确认 true / 取消 false；
 * 弹窗本体是 <ConfirmDialog/>（挂在 Providers），4 套主题、双语言、焦点圈闭都跟着
 * 应用走。window.confirm 是浏览器原生样式、阻塞主线程，且移动端弹的是系统框。 */

import { create } from "zustand";

export interface ConfirmOptions {
  message: string;
  title?: string;
  /** 不传时由弹窗按界面语言补默认文案（确定 / 取消） */
  confirmLabel?: string;
  cancelLabel?: string;
  /** 危险操作（删除类）：确认键用 danger 色 */
  danger?: boolean;
}

interface PendingConfirm extends ConfirmOptions {
  resolve: (value: boolean) => void;
}

interface ConfirmState {
  pending: PendingConfirm | null;
  request: (options: ConfirmOptions) => Promise<boolean>;
  settle: (value: boolean) => void;
}

export const useConfirmStore = create<ConfirmState>()((set, get) => ({
  pending: null,

  request: (options) =>
    new Promise<boolean>((resolve) => {
      // 并发请求：旧的按取消收场，不把 Promise 悬着（当前只有单用户手动触发，防患）
      get().pending?.resolve(false);
      set({ pending: { ...options, resolve } });
    }),

  settle: (value) => {
    const pending = get().pending;
    if (!pending) {
      return;
    }
    set({ pending: null });
    pending.resolve(value);
  },
}));

export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  return useConfirmStore.getState().request(options);
}
