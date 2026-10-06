/** P9 引用全链路（§7.15 / §14 P9）：write_note 落笔记、错题引用、记录搬家后引用不失效。
 *
 * 文件名排在 questions.e2e 之后，脚本步骤只追加在 chat_scenarios.yaml 末尾（Turn 25-28）。
 * 脚本按 LLM 调用序**全局**消费，所以本文件**不能单跑**（单跑会从头吃步骤，断言全错位）——
 * 跑法：`npx playwright test`（全量，与仓库既有约定一致）。
 *
 * 「回答用上了题目内容」这类注入断言在后端 tests/test_refs.py（脚本替身不回显输入，
 * 前端只能验链路：chip 出现、发得出去、深链落点对）。
 */

import fs from "node:fs";
import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const SESSION_TITLE = "解释傅里叶变换";

async function openSession(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/");
  await page.locator("aside").getByText(SESSION_TITLE, { exact: false }).first().click();
  await expect(page.locator("textarea").first()).toBeVisible();
}

test("① write_note 存记录：控制台可见、导出 Markdown 带得上（验收①）", async ({ page }) => {
  await openSession(page);
  const box = page.locator("textarea").first();
  await box.fill("把链式法则记到笔记本里");
  await box.press("Enter");

  // 模型调 write_note（工具卡可见）→ 收尾一句话
  await expect(page.getByText("write_note").first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("记好了：链式法则已经存进笔记本「速记」。")).toBeVisible({
    timeout: 10000,
  });

  // 控制台：默认本「速记」被工具自动建出，记录在里头（type=note）
  await page.goto("/notebooks");
  const card = page.getByTestId("nb-card").filter({ hasText: "速记" });
  await expect(card).toBeVisible({ timeout: 15000 });
  await card.locator("a").click();
  await page.waitForURL(/\/notebooks\/nb-[0-9a-f]{8}/, { timeout: 15000 });
  const record = page.getByTestId("nb-record").filter({ hasText: "链式法则" });
  await expect(record).toBeVisible({ timeout: 15000 });
  await expect(record.getByTestId("nb-record-type")).toHaveAttribute("data-type", "note");

  // 导出：下载的 md 里有标题也有正文（验收①的「可导出」）
  const download = page.waitForEvent("download");
  await page.getByTestId("nb-export").click();
  const saved = fs.readFileSync(await (await download).path(), "utf8");
  expect(saved).toContain("链式法则");
  expect(saved).toContain("复合函数求导");
});

test("② 错题引用：错题筛可见，「+」挂引用提问，消息 chip 点回置顶题卡（验收②）", async ({
  page,
}) => {
  // 造一道题并**刻意答错**（判分零 LLM）→ 进错题库
  await page.goto("/questions");
  await page.getByTestId("q-new").click();
  await page.getByTestId("q-new-form-stem").fill("（引用题丙）函数 $y=(2x+1)^2$ 的导数是多少？");
  await page.getByTestId("q-new-form-options").fill("4(2x+1)\n2(2x+1)\n4x\n2x");
  await page.getByTestId("q-new-form-answer").fill("A");
  await page.getByTestId("q-new-form-save").click();
  const card = page.getByTestId("q-card").filter({ hasText: "引用题丙" });
  await expect(card).toBeVisible({ timeout: 15000 });
  await card.locator('[data-testid="q-option"][data-label="B"]').click();
  await card.getByTestId("q-submit").click();
  await expect(card.getByTestId("q-result")).toHaveAttribute("data-correct", "false", {
    timeout: 15000,
  });
  // 题库页「错题」筛能看到它
  await page.getByTestId("q-filter-wrong").click();
  await expect(page.getByTestId("q-card").filter({ hasText: "引用题丙" })).toBeVisible();

  // 回聊天：「+」菜单 → 题库 tab（默认就是错题筛）→ 挑这道题
  await openSession(page);
  const box = page.locator("textarea").first();
  await expect(page.getByRole("button", { name: "发送" })).toBeDisabled(); // 空手不能发
  await page.getByTestId("ref-menu-btn").click();
  await expect(page.getByTestId("ref-menu")).toBeVisible();
  await page.getByTestId("ref-tab-question").click();
  await expect(page.getByTestId("ref-q-wrong")).toHaveAttribute("data-active", "true");
  const row = page.getByTestId("ref-question").filter({ hasText: "引用题丙" });
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();
  await page.keyboard.press("Escape"); // 关菜单；引用留在待发区

  // 待发 chip 出现，且只挂引用也够发（引用也算内容）
  const chip = page.getByTestId("ref-chip").filter({ hasText: "引用题丙" });
  await expect(chip).toBeVisible();
  await expect(page.getByRole("button", { name: "发送" })).toBeEnabled();

  await box.fill("这道题怎么算？");
  await box.press("Enter");
  await expect(page.getByText("这道题用链式法则")).toBeVisible({ timeout: 15000 });

  // 消息上带引用 chip，待发区已清空
  const userMsg = page.getByTestId("user-message").filter({ hasText: "这道题怎么算？" });
  const refLink = userMsg.getByTestId("msg-ref");
  await expect(refLink).toHaveCount(1);
  await expect(page.getByTestId("ref-chip")).toHaveCount(0);

  // 点 chip → 题库页把这道题置顶
  await refLink.click();
  await expect(page).toHaveURL(/\/questions\?question=q-[0-9a-f]{8}/, { timeout: 15000 });
  const pinned = page.getByTestId("q-ref-pinned");
  await expect(pinned).toBeVisible({ timeout: 15000 });
  await expect(pinned).toContainText("引用题丙");
});

test("③ 记录引用跨本不失效：搬家后 chip 深链跟到新本，重新生成照跑（验收③）", async ({ page }) => {
  // 先建目标本「乙本」（REST 建，零 LLM，不占脚本步骤）
  const created = await page.request.post("/api/v1/notebooks", { data: { name: "乙本" } });
  expect(created.ok()).toBeTruthy();
  const target = (await created.json()) as { id: string };

  // 聊天里引用「速记」中「链式法则」那条记录提问（发送时记录还在速记）
  await openSession(page);
  const box = page.locator("textarea").first();
  await page.getByTestId("ref-menu-btn").click();
  await page.getByTestId("ref-notebook").filter({ hasText: "速记" }).click();
  const row = page.getByTestId("ref-record").filter({ hasText: "链式法则" });
  await expect(row).toBeVisible({ timeout: 15000 });
  const recordId = await row.getAttribute("data-id");
  expect(recordId).toBeTruthy();
  await row.click();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("ref-chip").filter({ hasText: "链式法则" })).toBeVisible();

  await box.fill("这条记录再讲一遍");
  await box.press("Enter");
  await expect(page.getByText("这条记录讲的是链式法则")).toBeVisible({ timeout: 15000 });
  const userMsg = page.getByTestId("user-message").filter({ hasText: "这条记录再讲一遍" });
  await expect(userMsg.getByTestId("msg-ref")).toHaveCount(1);

  // 把记录从「速记」搬到「乙本」
  await page.goto("/notebooks");
  await page.getByTestId("nb-card").filter({ hasText: "速记" }).locator("a").click();
  await page.waitForURL(/\/notebooks\/nb-[0-9a-f]{8}/, { timeout: 15000 });
  const record = page.locator(`[data-record-id="${recordId}"]`);
  await expect(record).toBeVisible({ timeout: 15000 });
  await record.getByTestId("nb-record-menu").click();
  await page.getByTestId("nb-move-target").filter({ hasText: "乙本" }).click();
  await expect(record).toHaveCount(0, { timeout: 15000 }); // 搬走了：不在速记了

  // 回聊天点旧消息的 chip：快照里归属还是速记 → 页面自己跳到记录现在的家「乙本」，
  // 深链高亮也跟过去（跨本重定向 + ?record= 定位）
  await openSession(page);
  await page
    .getByTestId("user-message")
    .filter({ hasText: "这条记录再讲一遍" })
    .getByTestId("msg-ref")
    .click();
  await expect(page).toHaveURL(new RegExp(`/notebooks/${target.id}\\?record=${recordId}`), {
    timeout: 15000,
  });
  await expect(page.getByRole("heading", { name: "乙本" })).toBeVisible({ timeout: 15000 });
  const moved = page.locator(`[data-record-id="${recordId}"]`);
  await expect(moved).toBeVisible();
  await expect(moved).toHaveClass(/ring-primary/);

  // 重新生成：引用从末条 user 消息 metadata 重建后**活查**，搬了家照样找得到（回合不炸、
  // 新回答落地；注入内容本身由后端 test_refs.py 断言）
  await openSession(page);
  await page.getByTestId("regenerate").click();
  await expect(page.getByText("重新讲一遍这条记录")).toBeVisible({ timeout: 15000 });
});
