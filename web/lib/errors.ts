/** 页面级错误提示的统一口径（各页 `String(error)` 的替代）。
 *
 * 直接 `String(error)` 会把 `ApiError: xxx` 的名称前缀带出来，网络层失败更是
 * 一句英文的 `TypeError: Failed to fetch`——用户看不懂，看着像坏了。
 */

import { ApiError } from "@/lib/api";

export function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.message; // 后端 §9.1 信封里的 message，本来就是给人读的
  }
  if (error instanceof TypeError) {
    return fallback; // fetch 网络层失败（后端没起/断网）统一成一句人话
  }
  return String(error); // 其余原样透出，别把真话盖掉
}
