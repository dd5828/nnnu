/** P2 #9：7.1 聊天验收全场景（scripted 后端，零 API 成本）。
 *
 * 串行 + 单 worker：scripted 步骤按 LLM 调用序消费（见 fixtures/chat_scenarios.yaml）。
 * 会话跨测试共享（后端同一 NNNU_HOME），每个测试开新页面后先点会话列表进入。
 */

import path from "node:path";
import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const SESSION_TITLE = "解释傅里叶变换";

async function openSession(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/");
  await page.locator("aside").getByText(SESSION_TITLE, { exact: false }).first().click();
  await expect(page.locator("textarea").first()).toBeVisible();
}

test("① 流式一问一答：思考块与正文分离、KaTeX、成本摘要、会话标题", async ({ page }) => {
  await page.goto("/");
  const box = page.locator("textarea").first();
  await box.fill(SESSION_TITLE);
  await box.press("Enter");
  // 思考块在流式窗口内可见（脚本 delay 1500ms 留出观察窗）
  await expect(page.getByText("思考", { exact: true })).toBeVisible({ timeout: 5000 });
  // 回合结束：成本徽标出现
  await expect(page.getByText(/成本/).first()).toBeVisible({ timeout: 15000 });
  // 正文完整渲染 + KaTeX 公式
  await expect(page.getByText("傅里叶变换把时域信号分解为频域分量。")).toBeVisible();
  await expect(page.locator(".katex").first()).toBeVisible();
  // 会话标题 = 首条用户消息
  await expect(page.locator("aside").getByText(SESSION_TITLE).first()).toBeVisible();
});

test("② ask_user 中途提问弹窗：选择后回合继续", async ({ page }) => {
  await openSession(page);
  const box = page.locator("textarea").first();
  await box.fill("再展开讲讲");
  await box.press("Enter");
  // 弹窗出现：问题 + 两个选项按钮
  await expect(page.getByText("你想深入了解哪个方向？")).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "工程应用" }).click();
  // 回合继续并体现用户答复
  await expect(page.getByText("好，我们看工程应用方向")).toBeVisible({ timeout: 10000 });
});

test("③ 重新生成：旧回复被新回复替换", async ({ page }) => {
  await openSession(page);
  await page.getByRole("button", { name: "重新生成" }).first().click();
  await expect(page.getByText("重新生成的回答")).toBeVisible({ timeout: 10000 });
});

test("④ 停止：流式中断，1 秒内回到空闲", async ({ page }) => {
  await openSession(page);
  const box = page.locator("textarea").first();
  await box.fill("停下来");
  await box.press("Enter");
  // 脚本 8s 停顿保证停表窗口；500ms 后点停止
  await page.waitForTimeout(500);
  await page.getByRole("button", { name: "停止" }).first().click();
  // 发送按钮重新出现 = 回到空闲（§7.1 停止 1 秒内中断）
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible({ timeout: 2000 });
});

test("⑤ 附件 PDF 引用：上传后回答带引用来源", async ({ page }) => {
  await openSession(page);
  const pdf = path.resolve(__dirname, ".artifacts", "sample.pdf");
  await page.locator('input[type="file"]').setInputFiles(pdf);
  await expect(page.getByText("sample.pdf")).toBeVisible({ timeout: 10000 });
  const box = page.locator("textarea").first();
  await box.fill("总结这份文档");
  await box.press("Enter");
  // 模型调 attachment_search（工具卡可见）→ 引用面板出现 → 回答落地
  await expect(page.getByText("attachment_search").first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("引用来源")).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("根据你上传的文档")).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(/第 1 页/).first()).toBeVisible();
});

test("⑥ code_execution：沙箱真跑代码，stdout 回到对话", async ({ page }) => {
  await openSession(page);
  const box = page.locator("textarea").first();
  await box.fill("算一下 1 到 10 的和");
  await box.press("Enter");
  // 工具卡出现（code_execution 恒挂载）→ 展开看沙箱 stdout
  const card = page.getByRole("button").filter({ hasText: "code_execution" }).first();
  await expect(card).toBeVisible({ timeout: 10000 });
  await card.click();
  await expect(page.getByText(/sum-1-to-10=/).first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("沙箱算出来是 55。")).toBeVisible({ timeout: 10000 });
});

test("⑦ 工具开关：设置里放行 exec 后，下一回合真能调起来", async ({ page }) => {
  // 默认 exec 在「禁用工具」里 → 先在设置页改开关（应用即时生效）
  await page.goto("/settings");
  const card = page.locator("section").filter({ hasText: "禁用工具" });
  await expect(card.getByLabel("禁用工具")).toHaveValue("exec");
  // 工具目录列出全部内置工具（含被禁用的 exec），点开是参数 schema
  const catalog = page.locator("section").filter({ hasText: "工具目录" });
  await expect(catalog.getByText("exec", { exact: true })).toBeVisible({ timeout: 10000 });
  await expect(catalog.getByText("code_execution", { exact: true })).toBeVisible();
  // 联网搜索这类可开关工具默认就是挂着的，不在禁用列表里
  await expect(catalog.getByText("web_search", { exact: true })).toBeVisible();
  await card.getByLabel("禁用工具").fill(""); // 把 exec 从禁用列表删掉即放行
  await card.getByRole("button", { name: "应用" }).click();
  await expect(card.getByText("已应用")).toBeVisible({ timeout: 10000 });

  // 回对话：模型调 exec，命令真的跑了
  await openSession(page);
  const box = page.locator("textarea").first();
  await box.fill("跑个命令");
  await box.press("Enter");
  const execCard = page.getByRole("button").filter({ hasText: "exec" }).first();
  await expect(execCard).toBeVisible({ timeout: 10000 });
  await execCard.click();
  await expect(page.getByText("exec-ok").first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("命令跑通了，输出 exec-ok。")).toBeVisible({ timeout: 10000 });
});
