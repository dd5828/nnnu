/** 主题状态：Zustand + 裸字符串 localStorage（与 ThemeScript 共用键）。
 *  服务端渲染时不读存储（初始 null），挂载后水合，避免 hydration 不一致。 */

import { create } from "zustand";
import { isTheme, THEME_STORAGE_KEY, type Theme } from "@/lib/theme";

interface ThemeState {
  /** null = 尚未水合（跟随系统偏好），UI 可用 mounted 守卫规避闪烁 */
  theme: Theme | null;
  setTheme: (theme: Theme) => void;
  hydrateFromStorage: () => void;
}

export const useThemeStore = create<ThemeState>()((set) => ({
  theme: null,
  setTheme: (theme) => {
    set({ theme });
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // 存储不可用（隐私模式等），仅会话内生效
    }
  },
  hydrateFromStorage: () => {
    try {
      const stored = localStorage.getItem(THEME_STORAGE_KEY);
      if (isTheme(stored)) {
        set({ theme: stored });
      }
    } catch {
      // 同上
    }
  },
}));
