/** 子代理界面的纯函数（§7.12）：活动流之外的形状活儿都放这儿，方便单测。
 *
 * 组件测在本仓跑不起来（vitest 是 node 环境，只收 `tests/*.test.ts`，P5 批三的结论），
 * 所以能离开组件的计算一律先离开。 */

/** 工具参数 → 单行摘要（`path=src/a.py timeout=30`）：服务端已经把每个值截短了，
 *  这里只负责摊平成一行——JSON 原样塞进摘要行会把卡片撑成一坨多行文本。
 *  返回空串表示「没有可展示的参数」（调用方决定要不要留那个空格）。 */
export function argsLine(args: Record<string, unknown> | null): string {
  if (!args) {
    return "";
  }
  const parts: string[] = [];
  for (const [key, value] of Object.entries(args)) {
    const raw = typeof value === "string" ? value : JSON.stringify(value);
    parts.push(`${key}=${(raw ?? "").replace(/\s+/g, " ").trim()}`);
  }
  return parts.join(" ");
}
