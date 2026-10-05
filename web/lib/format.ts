/** 日期 / 数字的本地化口径：一律跟随**界面语言**，不是浏览器 locale。
 *
 * 反例：界面切到英文、浏览器还是中文时，`toLocaleString()` 会吐中文日期；
 * 读屏与字体渲染也跟着错。所有展示层格式化都从这里走。 */

import type { Language } from "@/i18n";

const LOCALES: Record<Language, string> = { zh: "zh-CN", en: "en-US" };

export function localeFor(lang: Language): string {
  return LOCALES[lang];
}

/** 秒级时间戳 → 本地日期时间；无效值给空串（列表不炸，与 formatTs 同口径）。 */
export function formatDateTime(seconds: number, lang: Language): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  return new Date(seconds * 1000).toLocaleString(localeFor(lang));
}

/** 秒级时间戳 → 本地日期（不含时刻）。 */
export function formatDate(seconds: number, lang: Language): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "";
  }
  return new Date(seconds * 1000).toLocaleDateString(localeFor(lang));
}

export function formatNumber(value: number, lang: Language): string {
  return value.toLocaleString(localeFor(lang));
}
