/** P9 题库增强（§7.4）：笔记、AI 分类、举一反三（相似题零 LLM；分类/出题吃脚本两次调用）。
 *
 * 文件名排序在所有 e2e 之后，脚本步骤只追加在 chat_scenarios.yaml 末尾（Turn 23/24）——
 * 绝不中间插入：脚本按 LLM 调用序全局消费，插中间会让既有场景全部错位。
 * **不单跑本文件**（同上原因，仓库既有约定）。
 */

import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

/** 造一道题（表单走一遍），返回它的卡片定位器。
 *
 * 定位用纯文本标记（题干里的 $…$ 会渲染成 KaTeX，原文不连续，别拿整句当 hasText）。
 */
async function createQuestion(
  page: import("@playwright/test").Page,
  label: string,
  stem: string,
  options: string,
  answer: string
) {
  await page.getByTestId("q-new").click();
  await page.getByTestId("q-new-form-stem").fill(stem);
  await page.getByTestId("q-new-form-options").fill(options);
  await page.getByTestId("q-new-form-answer").fill(answer);
  await page.getByTestId("q-new-form-knowledge-point").fill("导数");
  await page.getByTestId("q-new-form-save").click();
  const card = page.getByTestId("q-card").filter({ hasText: label });
  await expect(card).toBeVisible();
  return card;
}

test("① 建题 + 标签/错因 chips + 笔记存取 + 相似题（零 LLM）", async ({ page }) => {
  await page.goto("/questions");

  // 题目甲：带标签与错因（表单里的新字段）
  await page.getByTestId("q-new").click();
  await page
    .getByTestId("q-new-form-stem")
    .fill("（题目甲）函数 $f(x)=x^2$ 在 $x=2$ 处的导数是多少？");
  await page.getByTestId("q-new-form-options").fill("4\n2\n8\n6");
  await page.getByTestId("q-new-form-answer").fill("A");
  await page.getByTestId("q-new-form-knowledge-point").fill("导数");
  await page.getByTestId("q-new-form-tags").fill("易错, 常考");
  await page.getByTestId("q-new-form-cause-calculation").click();
  await page.getByTestId("q-new-form-save").click();
  const cardA = page.getByTestId("q-card").filter({ hasText: "题目甲" });
  await expect(cardA).toBeVisible();
  await expect(cardA.getByTestId("q-tag").filter({ hasText: "易错" })).toBeVisible();
  await expect(cardA.getByTestId("q-tag").filter({ hasText: "常考" })).toBeVisible();
  await expect(cardA.locator('[data-testid="q-cause"][data-cause="calculation"]')).toBeVisible();

  // 题目乙：同模板换个数——相似题的靶子
  const cardB = await createQuestion(
    page,
    "题目乙",
    "（题目乙）函数 $f(x)=x^2$ 在 $x=3$ 处的导数是多少？",
    "6\n9\n3\n12",
    "A"
  );
  const idB = await cardB.getAttribute("data-id");

  // 笔记：写 → 显示 → 刷新后还在（PATCH 落库）
  await cardA.getByTestId("q-note-edit").click();
  await page.getByTestId("q-note-input").fill("第一次做错：幂函数求导公式记混。");
  await page.getByTestId("q-note-save").click();
  await expect(cardA).toContainText("公式记混");
  await page.reload();
  await expect(page.getByTestId("q-card").filter({ hasText: "题目甲" })).toContainText("公式记混");

  // 相似题：打开面板即拉（零 LLM），乙必须出现在甲的相似列表里
  const cardAReloaded = page.getByTestId("q-card").filter({ hasText: "题目甲" });
  await cardAReloaded.getByTestId("q-variants-toggle").click();
  const panel = cardAReloaded.getByTestId("q-variants-panel");
  await expect(panel).toBeVisible();
  const similarB = panel.locator(`[data-testid="q-similar-item"][data-id="${idB}"]`);
  await expect(similarB).toBeVisible({ timeout: 15000 });
  await expect(similarB.getByTestId("q-similar-score")).toContainText("相似");

  // 错因筛选：乙没有错因，筛「计算失误」后应被滤掉
  await page.getByTestId("q-filter-cause").selectOption("calculation");
  await expect(page.getByTestId("q-card").filter({ hasText: "题目甲" })).toBeVisible();
  await expect(page.getByTestId("q-card").filter({ hasText: "题目乙" })).toHaveCount(0);
  await page.getByTestId("q-filter-cause").selectOption("");
  await expect(page.getByTestId("q-card").filter({ hasText: "题目乙" })).toBeVisible();
});

test("② AI 分类写回三件套，再预览变式题并勾选采纳（两次脚本调用按序）", async ({ page }) => {
  await page.goto("/questions");
  const cardA = page.getByTestId("q-card").filter({ hasText: "题目甲" });
  await expect(cardA).toBeVisible();

  // --- 第 1 次 LLM：分类（Turn 23）---
  await cardA.getByTestId("q-classify").click();
  await expect(cardA.getByTestId("q-classify-reason")).toBeVisible({ timeout: 15000 });
  await expect(cardA).toContainText("幂函数求导"); // 知识点被覆盖
  await expect(cardA.getByTestId("q-tag").filter({ hasText: "易错" })).toBeVisible(); // 标签合并
  // 错因是「非空则覆盖」：AI 的新判断取代表单里选的旧错因（不是合并）
  await expect(
    cardA.locator('[data-testid="q-cause"][data-cause="concept_unclear"]')
  ).toBeVisible();
  await expect(cardA.locator('[data-testid="q-cause"][data-cause="calculation"]')).toHaveCount(0);

  // --- 第 2 次 LLM：出变式题（Turn 24，mode=ai 不吃知识库状态）---
  await cardA.getByTestId("q-variants-toggle").click();
  const panel = cardA.getByTestId("q-variants-panel");
  await expect(panel).toBeVisible();
  await panel.getByTestId("q-variants-mode").selectOption("ai");
  await panel.getByTestId("q-variants-count").selectOption("3");

  const cardsBefore = await page.getByTestId("q-card").count();
  await panel.getByTestId("q-variants-generate").click();
  // 坏草稿被丢：预览只剩两条
  await expect(panel.getByTestId("q-draft")).toHaveCount(2, { timeout: 15000 });
  await expect(panel.getByTestId("q-variants-origin")).toHaveAttribute("data-origin", "ai");
  // 预览不落库：卡片数没变
  await expect(page.getByTestId("q-card")).toHaveCount(cardsBefore);

  // 去勾一条，采纳剩下一条
  await panel.getByTestId("q-draft-check").nth(1).uncheck();
  await panel.getByTestId("q-variants-adopt").click();
  await expect(panel.getByTestId("q-variants-done")).toContainText("已采纳 1 道", {
    timeout: 15000,
  });

  // 采纳入库：多一张卡，题干就是采纳的那条（甲）
  // 先收起面板再断言：甲的面板里「相似题」也会刷新出刚采纳的题，同段文字会撞两张卡
  await cardA.getByTestId("q-variants-toggle").click();
  await expect(page.getByTestId("q-card")).toHaveCount(cardsBefore + 1);
  await expect(page.getByTestId("q-card").filter({ hasText: "变式甲" })).toBeVisible();
  await expect(page.getByTestId("q-card").filter({ hasText: "变式乙" })).toHaveCount(0);
});
