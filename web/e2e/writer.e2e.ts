/** P9 Co-Writer（§7.13）：建文档 → 分屏预览（KaTeX/Mermaid）→ 自动保存刷新不丢 →
 *  键盘真实选区 → 扩写 → diff → accept 整串写回 / reject 一分不动 → 整篇存到笔记本。
 *
 * 文件名排在 refs.e2e 之后，脚本步骤只追加在 chat_scenarios.yaml 末尾（Turn 29-30）。
 * 脚本按 LLM 调用序**全局**消费，所以本文件**不能单跑**（单跑会从头吃步骤，断言全错位）——
 * 跑法：`npx playwright test`（全量，与仓库既有约定一致）。
 *
 * ② 里的「编辑器整串相等」是竞态守门员：accept 前若不 cancel 挂起的自动保存 PATCH，
 * 2 秒后它拿改写前的旧正文把刚写回的结果冲掉，整串比较立刻炸——这正是要防的 bug。
 */

import { expect, test, type Locator, type Page } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const DOC = [
  "# 傅里叶笔记",
  "",
  "公式 $F(\\omega)=\\int f(t)e^{-i\\omega t}dt$ 很关键。",
  "",
  "```mermaid",
  "graph TD",
  "  A[时域] --> B[频域]",
  "```",
  "",
  "北京是中国的首都。",
].join("\n");

const SENTENCE = "北京是中国的首都。";
/** 与 chat_scenarios.yaml Turn 29 一致（扩写结果）。 */
const EXPANDED = "北京是中国的首都，也是全国的政治、文化与国际交往中心，常住人口超过两千万。";
/** 与 Turn 30 一致（reject 用例里会出现、但不该落进文档）。 */
const SHRUNK = "北京是中国的政治中心。";
const AFTER_ACCEPT = DOC.replace(SENTENCE, EXPANDED);

async function openDoc(page: Page): Promise<Locator> {
  await page.goto("/co-writer");
  await page.getByTestId("cw-card").filter({ hasText: "傅里叶笔记" }).locator("a").click();
  await page.waitForURL(/\/co-writer\/cw-[0-9a-f]{8}/, { timeout: 15000 });
  return page.getByTestId("cw-editor");
}

/** 键盘真实选区：光标顶到文末，Shift+← 逐字往回选 n 个码点（不能用 API 造选区）。 */
async function selectTail(editor: Locator, n: number): Promise<void> {
  await editor.click();
  await editor.press("Control+End");
  for (let i = 0; i < n; i += 1) {
    await editor.press("Shift+ArrowLeft");
  }
}

test("① 建文档 → 分屏预览 KaTeX/Mermaid → 自动保存刷新不丢（验收②③）", async ({ page }) => {
  await page.goto("/co-writer");
  await page.getByTestId("cw-new").click();
  await page.getByTestId("cw-title").fill("傅里叶笔记");
  await page.getByTestId("cw-create").click();
  await page.waitForURL(/\/co-writer\/cw-[0-9a-f]{8}/, { timeout: 15000 });

  const editor = page.getByTestId("cw-editor");
  await editor.fill(DOC);

  // 预览吃的是「最近一次服务端确认过的正文」：katex / mermaid 出来 = 2s 防抖那次
  // PATCH 已经落库了（顺带验了 KaTeX 与 Mermaid 渲染，验收③）
  const preview = page.getByTestId("cw-preview");
  await expect(preview.locator(".katex").first()).toBeVisible({ timeout: 15000 });
  await expect(preview.getByTestId("mermaid-viewer").locator("svg")).toHaveCount(1, {
    timeout: 15000,
  });

  // 刷新不丢（验收②：写作中刷新内容还在）
  await page.reload();
  await expect(page.getByTestId("cw-editor")).toHaveValue(DOC, { timeout: 15000 });
});

test("② 键盘选段 → 扩写 → diff → accept 整串写回（验收①）", async ({ page }) => {
  const editor = await openDoc(page);
  await expect(editor).toHaveValue(DOC);

  await selectTail(editor, Array.from(SENTENCE).length);
  const toolbar = page.getByTestId("cw-toolbar");
  await expect(toolbar).toBeVisible();
  await expect(toolbar).toContainText(SENTENCE); // 选区同步进工具条了
  await expect(toolbar).toContainText("9 字");

  await page.getByTestId("cw-action-expand").click();
  const diff = page.getByTestId("cw-diff");
  await expect(diff).toBeVisible({ timeout: 30000 });
  await expect(diff).toContainText(EXPANDED); // Turn 29 的扩写结果进来了

  await page.getByTestId("cw-accept").click();
  // 整串相等：编辑器必须**一字不差**等于「原文档里那段换成新文本」
  await expect(editor).toHaveValue(AFTER_ACCEPT, { timeout: 15000 });

  // 再刷新：写回发生在服务端（不是只在本地替换）
  await page.reload();
  await expect(page.getByTestId("cw-editor")).toHaveValue(AFTER_ACCEPT, { timeout: 15000 });
});

test("③ 再扩写一次，reject 后正文一分不动（验收①）", async ({ page }) => {
  const editor = await openDoc(page);
  await expect(editor).toHaveValue(AFTER_ACCEPT);

  await selectTail(editor, Array.from(EXPANDED).length);
  await expect(page.getByTestId("cw-toolbar")).toContainText(EXPANDED);

  await page.getByTestId("cw-action-expand").click();
  const diff = page.getByTestId("cw-diff");
  await expect(diff).toBeVisible({ timeout: 30000 });
  await expect(diff).toContainText(SHRUNK); // Turn 30 的候选文本

  await page.getByTestId("cw-reject").click();
  await expect(diff).toHaveCount(0);
  await expect(editor).toHaveValue(AFTER_ACCEPT); // 还是原样
});

test("④ 整篇存到笔记本：记录类型 co_writer（§7.15 联动）", async ({ page }) => {
  const editor = await openDoc(page);
  await expect(editor).toHaveValue(AFTER_ACCEPT);

  // 限定在顶栏里点（预览里的 mermaid 渲染块也自带一个同名「存入笔记本」按钮）
  const topbar = page.getByTestId("cw-topbar");
  await topbar.getByTestId("save-to-notebook").click();
  await topbar.getByTestId("notebook-option").filter({ hasText: "速记" }).click();
  await expect(topbar.getByTestId("save-to-notebook")).toContainText("已存入");
  await expect(topbar.getByTestId("notebook-option")).toHaveCount(0); // 菜单关上

  await page.goto("/notebooks");
  await page.getByTestId("nb-card").filter({ hasText: "速记" }).locator("a").click();
  await page.waitForURL(/\/notebooks\/nb-[0-9a-f]{8}/, { timeout: 15000 });
  const record = page.getByTestId("nb-record").filter({ hasText: "傅里叶笔记" });
  await expect(record).toBeVisible({ timeout: 15000 });
  await expect(record.getByTestId("nb-record-type")).toHaveAttribute("data-type", "co_writer");
});
