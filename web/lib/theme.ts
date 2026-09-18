/** 四主题（§4）：浅色/米色/深色/玻璃。切换走 <html data-theme> + CSS 变量。 */

export type Theme = "light" | "beige" | "dark" | "glass";

export const THEMES: Theme[] = ["light", "beige", "dark", "glass"];

/** localStorage 键：存裸字符串（如 "dark"），ThemeScript 与主题 store 共用。 */
export const THEME_STORAGE_KEY = "nnnu-theme";

export function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as string[]).includes(value);
}

/** 与 ThemeScript 内联脚本同一逻辑源：存储值 → 生效主题（非法/未选回退系统偏好）。 */
export function resolveTheme(stored: string | null, prefersDark: boolean): Theme {
  if (isTheme(stored)) {
    return stored;
  }
  return prefersDark ? "dark" : "light";
}
