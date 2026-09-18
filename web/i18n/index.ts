/** 自研轻量 i18n（§10.1）：嵌套 JSON 字典 + dot-path 取值，缺键回退英文。
 *  纯函数、SSR 安全；语言状态由 language-store 管理。 */

import en from "@/locales/en.json";
import zh from "@/locales/zh.json";

export type Language = "zh" | "en";

export const LANGUAGES: Language[] = ["zh", "en"];

const DICTIONARIES = { en, zh } as const;

type Dict = typeof en;

function lookup(dict: Dict, path: string): string | undefined {
  let node: unknown = dict;
  for (const part of path.split(".")) {
    if (typeof node !== "object" || node === null || !(part in node)) {
      return undefined;
    }
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

/** 翻译：{lang} 缺键回退英文，仍缺返回键名；支持 {var} 插值。 */
export function translate(lang: Language, path: string, vars?: Record<string, string>): string {
  let text = lookup(DICTIONARIES[lang], path) ?? lookup(DICTIONARIES.en, path) ?? path;
  if (vars) {
    text = text.replace(/\{(\w+)\}/g, (_match, key: string) => vars[key] ?? `{${key}}`);
  }
  return text;
}
