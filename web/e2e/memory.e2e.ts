/** P8 记忆工作台（§7.10）：三层浏览、条目编辑、证据链展开、手动整合、图谱模式切换。
 *
 * 夹具在 start-backend.mjs（L1 轨迹 ← L2 条目 ← L3 画像 + semantic.json + 记忆设置：
 * 自动整合关、四预算 0）——所以「立即整合」是零 LLM 通路，不吃 scripted 步骤。
 */

import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test("① 三层浏览：画像 → 事实 → 轨迹，都是夹具里的内容", async ({ page }) => {
  await page.goto("/memory");
  await expect(page.getByRole("heading", { name: "记忆" })).toBeVisible();

  // 概览统计砖：L1 行数要真数得出来（回归过文件名拼接错恒 0；聊天场景会往同月文件加行，
  // 断「不少于夹具的 4 行」，不钉具体数）
  const l1StatText = (await page.getByTestId("memory-stat-l1").textContent()) ?? "";
  const l1Lines = Number(l1StatText.match(/([1-9]\d*)\s*(行|lines)/)?.[1]);
  expect(l1Lines).toBeGreaterThanOrEqual(4);

  // L3 默认选中 profile：条目在
  await expect(page.getByTestId("memory-entry").filter({ hasText: "工程落地取向" })).toBeVisible();

  // L2：切到 chat 分面，事实条目在（默认就是 chat）
  await page.getByTestId("memory-tab-l2").click();
  await expect(page.getByTestId("memory-entry").filter({ hasText: "信号处理" })).toBeVisible();

  // L1：轨迹行在（夹具四行；同月文件里还有聊天场景跑出来的行，按内容定位不按计数）
  await page.getByTestId("memory-tab-l1").click();
  const rows = page.getByTestId("l1-row");
  await expect(rows.first()).toBeVisible();
  await expect(rows.filter({ hasText: "信号处理方向的学习" }).first()).toBeVisible();
  await expect(rows.filter({ hasText: "找到 3 条入门资料" }).first()).toBeVisible();
});

test("② 人工编辑 L2：改完进保护集，刷新后还在", async ({ page }) => {
  await page.goto("/memory");
  await page.getByTestId("memory-tab-l2").click();
  const entry = page.getByTestId("memory-entry").filter({ hasText: "信号处理" });
  await entry.getByTestId("memory-edit").click();
  await page.getByTestId("memory-edit-input").fill("我在做信号处理方向的学习（人工改过）");
  await page.getByTestId("memory-edit-save").click();
  await expect(page.getByTestId("memory-entry").filter({ hasText: "人工改过" })).toBeVisible();
  await expect(
    page.getByTestId("memory-entry").filter({ hasText: "人工改过" }).getByText("人工编辑")
  ).toBeVisible();

  // 落盘：刷新后仍是改过的正文（state.json 记了保护哈希）
  await page.reload();
  await page.getByTestId("memory-tab-l2").click();
  await expect(page.getByTestId("memory-entry").filter({ hasText: "人工改过" })).toBeVisible();
});

test("③ 证据链：点 L3 节点展开到 L2 → L1 原始事件", async ({ page }) => {
  await page.goto("/memory");
  const graph = page.getByTestId("memory-graph");
  await expect(graph.getByTestId("graph-node-list")).toBeVisible();

  // 全景先只有条目节点（不自动拉 L1）
  await expect(graph.locator('[data-testid="graph-node"][data-layer="l1"]')).toHaveCount(0);
  await expect(graph.locator('[data-testid="graph-node"][data-layer="l3"]')).toHaveCount(1);

  // 点 L3 节点（清单行与图节点同一语义）→ depth=2：L2 与 L1 都进来
  await graph.locator('[data-testid="graph-node-row"][data-layer="l3"]').first().click();
  await expect(graph.locator('[data-testid="graph-node"][data-layer="l2"]').first()).toBeVisible();
  await expect(graph.locator('[data-testid="graph-node"][data-layer="l1"]').first()).toBeVisible();
  await expect(
    graph.locator('[data-testid="graph-node-row"][data-layer="l1"]').filter({
      hasText: "我在做信号处理方向的学习，常用 Python。",
    })
  ).toBeVisible();

  // 返回全景：L1 节点收回
  await graph.getByTestId("graph-reset").click();
  await expect(graph.locator('[data-testid="graph-node"][data-layer="l1"]')).toHaveCount(0);
});

test("④ 设置页记忆区：阈值与预算字段在（spec 驱动渲染）", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByText("三层记忆的整合阈值与 LLM 预算", { exact: false })).toBeVisible();
  await expect(page.getByLabel("自动整合", { exact: true })).toBeVisible();
  await expect(page.getByLabel("自动整合阈值（回合）", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Update 预算", { exact: true })).toBeVisible();
});

test("⑤ 手动整合（零 LLM）：跑完落账「完成」", async ({ page }) => {
  await page.goto("/memory");
  await expect(page.getByTestId("memory-last-run")).toContainText("还没整合过");
  await page.getByTestId("memory-consolidate").click();
  await expect(page.getByTestId("memory-last-run")).toContainText("完成", { timeout: 15000 });
});

test("⑥ 图谱模式切换：知识图谱显示实体/关系，切回证据链复现", async ({ page }) => {
  await page.goto("/memory");
  const graph = page.getByTestId("memory-graph");
  await expect(graph.getByTestId("graph-node-list")).toBeVisible(); // 默认证据链

  // 切知识图谱：夹具 semantic.json 的两个实体 + 一条关系
  await graph.getByTestId("memory-graph-mode-semantic").click();
  const entityRow = graph.getByTestId("semantic-entity-row").filter({ hasText: "信号处理" });
  await expect(entityRow).toBeVisible();
  await expect(entityRow).toHaveAttribute("data-type", "topic"); // 类型文字编码（非仅颜色）
  await expect(
    graph.getByTestId("semantic-relation-row").filter({ hasText: "采样定理" })
  ).toBeVisible();
  // 不断言 stale 徽标：用例②改过条目，全量跑时工件必然过期（顺序耦合）

  // 切回证据链：清单复现（root 已清回全景）
  await graph.getByTestId("memory-graph-mode-evidence").click();
  await expect(graph.getByTestId("graph-node-list")).toBeVisible();
});
