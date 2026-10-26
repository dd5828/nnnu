/** P14 知识中心外部库与引擎选择（§7.9）：不调模型，纯本地文件与界面往返。
 *
 * 场景：
 *  ① Obsidian——现场建一个临时 vault → 挂载 → 读到原笔记 → 在界面上改 → 保存后原文件变化；
 *  ② 引擎选择——建库向导列出四个引擎（不可用的也列出来），能选中 PageIndex。
 *
 * 文件名 zzzzz- 打头，排在全量 E2E 最后；没有 LLM 调用，不吃脚本步骤。
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const VAULT = fs.mkdtempSync(path.join(os.tmpdir(), "nnnu-e2e-vault-"));
const NOTE = "e2e-note.md";
const INITIAL = "# E2E 笔记\n\n原始内容。\n";
const UPDATED = `${INITIAL}\nE2E 写回的内容。\n`;

function samePath(left: string, right: string): boolean {
  const normalize = (value: string) => path.resolve(value).replaceAll("\\", "/").toLowerCase();
  return normalize(left) === normalize(right);
}

test.beforeAll(() => {
  fs.writeFileSync(path.join(VAULT, NOTE), INITIAL, "utf8");
});

test.afterAll(() => {
  fs.rmSync(VAULT, { recursive: true, force: true });
});

test("① Obsidian：挂载 vault → 读笔记 → 改完写回原文件", async ({ page, request }) => {
  await page.goto("/knowledge");
  const panel = page.getByTestId("external-libraries");
  await expect(panel).toBeVisible();

  await page.getByTestId("obsidian-add").click();
  await page.getByTestId("obsidian-vault-path").fill(VAULT);
  await page.getByTestId("obsidian-vault-add").click();

  const noteButton = page.locator(`[data-testid="obsidian-note"][data-path="${NOTE}"]`);
  await expect(noteButton).toBeVisible({ timeout: 15000 });
  await noteButton.click();

  const editor = page.getByTestId("obsidian-editor");
  await expect(editor).toHaveValue(INITIAL, { timeout: 10000 });
  await editor.fill(UPDATED);
  await page.getByTestId("obsidian-save").click();
  await expect(page.getByText("已写回原文件")).toBeVisible({ timeout: 10000 });
  await expect.poll(() => fs.readFileSync(path.join(VAULT, NOTE), "utf8")).toBe(UPDATED);

  // 收尾：把这个临时挂载摘掉，别让 .e2e-home 越跑越脏
  const vaults = await request.get("/api/v1/libraries/obsidian/vaults");
  const body = (await vaults.json()) as { vaults: { vault_id: string; path: string }[] };
  for (const vault of body.vaults) {
    if (samePath(vault.path, VAULT)) {
      await request.delete(`/api/v1/libraries/obsidian/vaults/${vault.vault_id}`);
    }
  }
});

test("② 建库向导列出四个检索引擎，能选中 PageIndex", async ({ page }) => {
  await page.goto("/knowledge");
  await page.getByTestId("kb-new").click();
  const select = page.getByTestId("kb-engine");
  await expect(select).toBeVisible();

  const values = await select
    .locator("option")
    .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value));
  expect(values).toEqual(["vector", "pageindex", "lightrag", "graphrag"]);

  await select.selectOption("pageindex");
  await expect(select).toHaveValue("pageindex");
});
