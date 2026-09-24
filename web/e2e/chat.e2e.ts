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

test("⑧ 知识库：建库上传到就绪，测试台检索命中第 1 页", async ({ page }) => {
  const pdf = path.resolve(__dirname, ".artifacts", "sample.pdf");
  await page.goto("/knowledge");
  await page.getByTestId("kb-new").click();
  await page.getByTestId("kb-name").fill("信号库");
  await page.getByTestId("kb-file-input").setInputFiles(pdf);
  await page.getByTestId("kb-create-submit").click();

  // 向导：上传字节进度 → 索引进度 → 索引完成自动进详情页
  await expect(page.getByTestId("kb-create-progress")).toBeVisible({ timeout: 20000 });
  await page.waitForURL(/\/knowledge\/kb-/, { timeout: 60000 });
  const row = page.getByTestId("doc-row").first();
  await expect(row).toContainText("sample.pdf");
  await expect(row.getByTestId("doc-status")).toHaveAttribute("data-status", "done", {
    timeout: 30000,
  });

  // 检索测试台：混合检索命中，且命中带页码
  await page.getByTestId("search-query").fill("傅里叶变换");
  await page.getByTestId("search-mode").selectOption("hybrid");
  await page.getByTestId("search-run").click();
  const hit = page.getByTestId("search-hit").first();
  await expect(hit).toBeVisible({ timeout: 20000 });
  await expect(hit.getByTestId("hit-page")).toHaveText(/第 1 页/);
  // 分数是 RRF 融合分，不该是 0（修过一次全 0 的坑）
  await expect(hit).not.toContainText("分 0.0000");
});

test("⑨ 知识库引用可点：从引用面板跳进阅读器并定位页码", async ({ page }) => {
  await openSession(page);
  // 先在输入区选库（§7.9：不选就不挂 rag，默认不检索知识库）
  await page.getByTestId("kb-selector").click();
  await page.getByTestId("kb-option").filter({ hasText: "信号库" }).click();
  await expect(page.getByTestId("kb-selector")).toContainText("信号库");
  const box = page.locator("textarea").first();
  await box.fill("查一下知识库里的傅里叶变换");
  await box.press("Enter");
  // 模型调 rag（选了库才挂载）→ 引用面板出现
  await expect(page.getByText("rag", { exact: true }).first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByText("引用来源").first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByText("知识库里说，傅里叶变换把时域信号分解为频域分量")).toBeVisible({
    timeout: 15000,
  });

  // 点知识库引用 → 知识中心详情页 + 阅读器开到第 1 页
  await page.getByTestId("citation-link").last().click();
  await expect(page).toHaveURL(/\/knowledge\/kb-.*[?&]doc=.*[?&]page=1/, { timeout: 15000 });
  const reader = page.getByTestId("reader");
  await expect(reader).toBeVisible({ timeout: 15000 });
  await expect(reader.getByText("第 1 页")).toBeVisible();
  // PDF 走 iframe 页锚
  await expect(reader.locator("iframe")).toHaveAttribute("src", /#page=1$/);
});

test("⑩ 知识库 Markdown 预览：正文渲染成 DOM，不再是解析文本", async ({ page }) => {
  const md = path.resolve(__dirname, "fixtures", "sample.md");
  await page.goto("/knowledge");
  await page.getByTestId("kb-card").filter({ hasText: "信号库" }).locator("a").click();
  await page.waitForURL(/\/knowledge\/kb-/, { timeout: 15000 });

  // 加一份 md 文档，等它进入就绪
  await page.getByTestId("kb-add-files").setInputFiles(md);
  const row = page.getByTestId("doc-row").filter({ hasText: "sample.md" });
  await expect(row).toBeVisible({ timeout: 15000 });
  await expect(row.getByTestId("doc-status")).toHaveAttribute("data-status", "done", {
    timeout: 30000,
  });

  // 点文件名开阅读器：拿原件文本走 Markdown 渲染
  await row.getByRole("button").first().click();
  const reader = page.getByTestId("reader");
  await expect(reader).toBeVisible({ timeout: 15000 });
  await expect(reader.getByRole("heading", { name: "傅里叶变换速查" })).toBeVisible({
    timeout: 15000,
  });
  await expect(reader.locator(".markdown-body")).toBeVisible();
  // 渲染成了 DOM；走 <pre> 兜底就算退化
  await expect(reader.locator("pre")).toHaveCount(0);
  await expect(reader.getByTestId("reader-download")).toBeVisible();
});

test("⑪ 模型选择：会话级粘性，切走再回来仍在，可清回默认", async ({ page }) => {
  await page.goto("/");
  // 新开会话：上一个会话在⑨里挂了知识库，粘性会跟过来，不掺和这条用例
  await page.getByRole("button", { name: "新对话" }).click();
  const box = page.locator("textarea").first();
  await expect(box).toBeVisible();

  // 选中一个模型（默认列表里 deepseek 行可点：start-backend 已 seed 假密钥）
  await page.getByTestId("model-selector").click();
  await page.locator('[data-testid="model-option"][data-value="deepseek:deepseek-chat"]').click();
  await expect(page.getByTestId("model-selector")).toContainText("deepseek-chat");

  // 粘性靠回合落库（§6.10）：发一条
  await box.fill("换个模型再问一次");
  await box.press("Enter");
  await expect(page.getByText("模型选择验证回答。")).toBeVisible({ timeout: 15000 });

  // 切到老会话（它没选过模型）再切回来：显示来自库里水合，不是内存残留
  await page.locator("aside").getByText(SESSION_TITLE, { exact: false }).first().click();
  await expect(page.getByTestId("model-selector")).toContainText("跟随设置默认");
  await page.locator("aside").getByText("换个模型再问一次").first().click();
  await expect(page.getByTestId("model-selector")).toContainText("deepseek-chat");

  // 显式清回默认
  await page.getByTestId("model-selector").click();
  await page.getByTestId("model-option-default").click();
  await expect(page.getByTestId("model-selector")).toContainText("跟随设置默认");
});

test("⑫ 解题能力：三阶段步骤条依次点亮，答案存进笔记本", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新对话" }).click();
  const box = page.locator("textarea").first();
  await expect(box).toBeVisible();

  // 输入区切到「解题」（§6.4 会话级粘性：随消息下发）
  await page.getByTestId("capability-selector").click();
  await page.locator('[data-testid="capability-option"][data-value="deep_solve"]').click();
  await expect(page.getByTestId("capability-selector")).toContainText("解题");

  await box.fill("求 d/dx[sin(x²)]");
  await box.press("Enter");

  // 步骤条：三个阶段一次全渲染，当前阶段依次点亮（脚本每段 1.2s 停顿）
  await expect(page.getByTestId("stage-bar")).toBeVisible({ timeout: 10000 });
  await expect(page.getByTestId("stage-pill")).toHaveCount(3);
  await expect(page.locator('[data-testid="stage-pill"][data-stage="reasoning"]')).toHaveAttribute(
    "data-state",
    "active",
    { timeout: 10000 }
  );
  await expect(page.locator('[data-testid="stage-pill"][data-stage="writing"]')).toHaveAttribute(
    "data-state",
    "active",
    { timeout: 15000 }
  );

  // 三段小标题（各段正文标题）+ 最终答案 + KaTeX
  await expect(page.getByRole("heading", { name: "解题规划" })).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("heading", { name: "详细推导" })).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("教学级解答：最终答案 2x·cos(x²)。")).toBeVisible({
    timeout: 20000,
  });
  await expect(page.locator(".katex").first()).toBeVisible();

  // 存入笔记本：一本都没有时「新建一本并存入」直接兜住
  await page.getByTestId("save-to-notebook").first().click();
  await page.getByTestId("notebook-create-and-save").click();
  await expect(page.getByTestId("save-to-notebook").first()).toContainText("已存入", {
    timeout: 10000,
  });

  // 笔记本页看得到这条记录（列表卡片 → 详情记录，正文是那份教学级解答）
  await page.goto("/notebooks");
  await expect(page.getByTestId("nb-card").first()).toBeVisible({ timeout: 10000 });
  await page.getByTestId("nb-card").first().click();
  await expect(page.getByTestId("nb-record").first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByTestId("nb-record").first()).toContainText("解题规划");
});
