/** P10 活书引擎（§7.14）：三份文档建库 → 建书与 spine 改名 → 编译全书 → 阅读器
 *  （答题判分/书签/页聊天带引用/导出 Markdown）→ 源漂移横幅 → 块编辑工具条。
 *
 * 文件名 zz- 打头，排在 writer.e2e 之后；脚本步骤只追加在 chat_scenarios.yaml
 * 末尾（Turn 31-39）。脚本按 LLM 调用序**全局**消费，所以本文件**不能单跑**
 * （单跑会从头吃步骤，断言全错位）——跑法：`npx playwright test`（全量，与仓库
 * 既有约定一致）。
 *
 * LLM 调用序（编译按章节序 × plan 序串行，见 src/nnnu/book/compile.py）：
 *   Turn 31 建书 spine；32-37 六个块（ch-1 text/quiz/figure、ch-2 text/flashcard、
 *   ch-3 text）；38/39 页聊天（先 rag 一步工具，再带出处回答）。
 *   估算/健康/导出/答题/块编辑都零 LLM，不占脚本步骤。
 */

import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";

test.describe.configure({ mode: "serial" });

/** §7.7 验收同款：收集主框架的 console error（沙箱 iframe 里用户页面的 CDN 不算）。 */
function trackConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && !msg.location().url.startsWith("about:")) {
      errors.push(msg.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

const BOOK_TITLE = "傅里叶小书";
const KB_NAME = "傅里叶素材库";
/** 第二章改名后的标题（① 改完，③ 的导出里要看到这个新名字）。 */
const RENAMED = "频谱的三种用法";
const CH1 = "时域与频域";
const CH3 = "从傅里叶到今天";

/** 文档内容内联在测试里（仓库约定：md 不入库，放文件会让全新检出跑不到）。 */
const DOC1 = [
  "# 时域与频域",
  "",
  "傅里叶变换把时域信号分解为频域分量：时域看每个时刻的振幅，频域看每个频率分量的多少。",
  "",
  "## 要点",
  "",
  "- 时域横轴是时间",
  "- 频域横轴是频率",
  "- 两者是同一段信号的两组坐标",
].join("\n");
const DOC2 = [
  "# 频谱能做什么",
  "",
  "滤波：在频域里压低不想要的频率分量。降噪与压缩也都依赖频谱分析。",
].join("\n");
const DOC3 = [
  "# 从傅里叶到今天",
  "",
  "1822 年傅里叶提出用三角级数表示温度分布；1965 年快速傅里叶变换（FFT）把计算量降到对数级。",
].join("\n");
/** 漂移用例追加的第四份文档。 */
const DOC4 = [
  "# 补充材料",
  "",
  "采样定理：采样频率要高于信号最高频率的两倍，频谱才不会混叠。",
].join("\n");

/** 跨用例留下的服务端数据定位（serial 跑，后一条依赖前一条的产物）。 */
let kbPath = "";
let bookId = "";

test("① 三份文档建库 → 建书选库 → spine 三章 → 改名 → 估算有数", async ({ page }) => {
  // 三份一次传完（文件框是 multiple）；向导走完自动进详情页
  await page.goto("/knowledge");
  await page.getByTestId("kb-new").click();
  await page.getByTestId("kb-name").fill(KB_NAME);
  await page.getByTestId("kb-file-input").setInputFiles([
    { name: "01-时域与频域.md", mimeType: "text/markdown", buffer: Buffer.from(DOC1, "utf-8") },
    { name: "02-频谱能做什么.md", mimeType: "text/markdown", buffer: Buffer.from(DOC2, "utf-8") },
    {
      name: "03-从傅里叶到今天.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(DOC3, "utf-8"),
    },
  ]);
  await page.getByTestId("kb-create-submit").click();
  await page.waitForURL(/\/knowledge\/kb-/, { timeout: 60000 });
  kbPath = new URL(page.url()).pathname;
  for (const name of ["01-时域与频域.md", "02-频谱能做什么.md", "03-从傅里叶到今天.md"]) {
    const row = page.getByTestId("doc-row").filter({ hasText: name });
    await expect(row.getByTestId("doc-status")).toHaveAttribute("data-status", "done", {
      timeout: 30000,
    });
  }

  // 建书：勾我们刚建的库（列表里还有别的用例建的库，按名字收窄）
  await page.goto("/book");
  await page.getByTestId("bk-new").click();
  await page.getByTestId("bk-title").fill(BOOK_TITLE);
  await page.getByTestId("bk-src-kb").filter({ hasText: KB_NAME }).locator("input").check();
  await page.getByTestId("bk-create").click();
  await page.waitForURL(/\/book\/bk-[0-9a-f]{8}/, { timeout: 30000 });
  bookId = /\/book\/(bk-[0-9a-f]{8})/.exec(page.url())![1];

  // spine 落地：三个章节行 + 估算面板有数
  await expect(page.getByTestId("bk-chapter")).toHaveCount(3, { timeout: 15000 });
  await expect(page.getByTestId("bk-estimate")).toBeVisible();

  // 改第二章名（输入框失焦提交）→ 估算跟着服务端重算
  const title2 = page.locator(
    '[data-testid="bk-chapter"][data-key="ch-2"] [data-testid="bk-chapter-title"]'
  );
  await title2.fill(RENAMED);
  await title2.press("Enter");
  await expect(page.getByTestId("bk-estimate-chapter").filter({ hasText: RENAMED })).toHaveCount(
    1,
    { timeout: 15000 }
  );
  await expect(page.getByTestId("bk-estimate-chapter")).toHaveCount(3);
  await expect(page.getByTestId("bk-estimate-tokens")).toHaveText(/\d/);
});

test("② 编译：看板轮询到 ready，六块全部落定（零坏块）", async ({ page }) => {
  await page.goto(`/book/${bookId}`);
  await page.getByTestId("bk-compile-start").click();

  // 202 之前状态已写 compiling（start() 先落库再起任务）→ 看板必现，轮询到 ready
  await expect(page.getByTestId("bk-compile")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("bk-read")).toBeVisible({ timeout: 45000 });

  // 目录三章、零失败块（有坏块会给「重编」按钮）
  await expect(page.getByTestId("bk-toc-row")).toHaveCount(3);
  await expect(page.getByTestId("bk-toc-recompile")).toHaveCount(0);

  // 后端真源再核一遍：每页每块都 done
  const detail = (await (await page.request.get(`/api/v1/books/${bookId}`)).json()) as {
    status: string;
    pages: { blocks: { status: string }[] }[];
  };
  expect(detail.status).toBe("ready");
  for (const item of detail.pages) {
    for (const block of item.blocks) {
      expect(block.status).toBe("done");
    }
  }
});

test("③ 阅读器：答题判分/书签/页聊天引用/导出，console 干净", async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page);
  await page.goto(`/book/${bookId}`);
  await page.getByTestId("bk-read").click();
  await page.waitForURL(/\/book\/bk-[0-9a-f]{8}\/bp-[0-9a-f]{8}/, { timeout: 15000 });

  // 验收「quiz/flashcard/figure 各 ≥1」：这一页是 text/quiz/figure，flashcard 在下一章
  await expect(page.getByTestId("bk-block")).toHaveCount(3, { timeout: 15000 });
  await expect(page.locator('[data-testid="bk-block"][data-type="quiz"]')).toHaveCount(1);
  await expect(page.locator('[data-testid="bk-block"][data-type="figure"]')).toHaveCount(1);
  // figure 块里的 mermaid 真渲染成 svg（payload 就是含围栏的正文，复用聊天渲染层）
  await expect(
    page
      .locator('[data-testid="bk-block"][data-type="figure"]')
      .getByTestId("mermaid-viewer")
      .locator("svg")
  ).toHaveCount(1, { timeout: 20000 });

  // 答题：选 A → 服务端判分 → 答对；刷新回来还显示已答（最近一次作答随页详情带回）
  await page.locator('[data-testid="bk-quiz-option"][data-key="A"]').click();
  await page.getByTestId("bk-quiz-submit").click();
  await expect(page.getByTestId("bk-quiz-result")).toHaveAttribute("data-correct", "true", {
    timeout: 15000,
  });
  await page.reload();
  await expect(page.getByTestId("bk-quiz-result")).toHaveAttribute("data-correct", "true", {
    timeout: 15000,
  });

  // 书签开关落库
  const bookmark = page.getByTestId("bk-bookmark");
  await bookmark.click();
  await expect(bookmark).toHaveAttribute("data-on", "true", { timeout: 15000 });

  // 页聊天：先检索（rag）再答，回答带对来源文档的引用（验收「页码对话能引用来源文档」）
  await page.getByTestId("bk-chat-input").fill("时域和频域是什么关系？");
  await page.getByTestId("bk-chat-send").click();
  const assistant = page.locator('[data-testid="bk-chat-message"][data-role="assistant"]');
  await expect(assistant).toBeVisible({ timeout: 30000 });
  const citation = assistant.getByTestId("citation-link").first();
  await expect(citation).toBeVisible({ timeout: 15000 });
  await expect(citation).toContainText(".md"); // 引到的是库里那份文档

  // 翻到第二章：flashcard 块 + 翻卡
  await page.getByTestId("bk-next").click();
  await expect(page.locator('[data-testid="bk-block"][data-type="flashcard"]')).toHaveCount(1, {
    timeout: 15000,
  });
  const card = page.getByTestId("bk-card");
  await expect(card).toHaveAttribute("data-side", "front");
  await card.click();
  await expect(card).toHaveAttribute("data-side", "back");

  // 回控制台：已读勾 + 书签标记都落上了（进度是真落库的）
  await page.goto(`/book/${bookId}`);
  const firstRow = page.locator('[data-testid="bk-toc-row"][data-key="ch-1"]');
  await expect(firstRow.locator("svg.text-success")).toHaveCount(1, { timeout: 15000 });
  await expect(firstRow.locator("svg.text-primary")).toHaveCount(1);

  // 导出 Markdown：三章标题与正文/题目都在（验收「导出结构完整」）
  const download = page.waitForEvent("download");
  await page.getByTestId("bk-export").click();
  const saved = fs.readFileSync(await (await download).path(), "utf8");
  expect(saved).toContain(CH1);
  expect(saved).toContain(RENAMED);
  expect(saved).toContain(CH3);
  expect(saved).toContain("傅里叶变换把时域信号分解为频域分量");
  expect(saved).toContain("频谱图的横轴");

  expect(consoleErrors).toEqual([]);
});

test("④ 源漂移：给库加第 4 份文档 → 书页健康横幅提示（验收漂移）", async ({ page }) => {
  await page.goto(kbPath);
  await page.getByTestId("kb-add-files").setInputFiles({
    name: "04-补充材料.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(DOC4, "utf-8"),
  });
  const row = page.getByTestId("doc-row").filter({ hasText: "04-补充材料.md" });
  await expect(row.getByTestId("doc-status")).toHaveAttribute("data-status", "done", {
    timeout: 30000,
  });

  // ready 后书页已停轮询：重载一次拿新算的健康（零 LLM）
  await page.goto(`/book/${bookId}`);
  const banner = page.getByTestId("bk-health");
  await expect(banner).toBeVisible({ timeout: 15000 });
  const item = banner.getByTestId("bk-drift-item").filter({ hasText: "04-补充材料.md" });
  await expect(item).toHaveCount(1);
  await expect(item).toHaveAttribute("data-change", "added");
});

test("⑤ 块编辑：手改正文、下移换序、插一条手写笔记再删（零 LLM）", async ({ page }) => {
  await page.goto(`/book/${bookId}`);
  await page.getByTestId("bk-read").click();
  await page.waitForURL(/\/book\/bk-[0-9a-f]{8}\/bp-[0-9a-f]{8}/, { timeout: 15000 });

  // 手改正文：编辑 → 落笔（update 同步 200，成功后页数据失效重取）
  // 工具条是悬浮显形（group-hover），先 hover 块本身再点按钮（真实操作路径）
  const text = page.locator('[data-testid="bk-block"][data-type="text"]').first();
  await text.hover();
  await text.getByTestId("bk-block-edit").click();
  await page
    .getByTestId("bk-block-editor")
    .fill("傅里叶变换就是时域与频域之间的翻译（这条是手改的）。");
  await page.getByTestId("bk-block-save").click();
  await expect(page.getByTestId("bk-block-editor")).toHaveCount(0, { timeout: 15000 });
  await expect(text).toContainText("手改的");

  // 下移：与 quiz 换位（相邻交换）
  await text.hover();
  await text.getByTestId("bk-block-down").click();
  await expect(page.locator('[data-testid="bk-block"]').first()).toHaveAttribute(
    "data-type",
    "quiz",
    { timeout: 15000 }
  );

  // 插入手写笔记：选 note → 直接进编辑态（零 LLM）→ 写完保存
  const quizBlock = page.locator('[data-testid="bk-block"]').first();
  await quizBlock.hover();
  await quizBlock.getByTestId("bk-block-insert").click();
  await page.locator('[data-testid="bk-type-item"][data-type="note"]').click();
  await page.getByTestId("bk-block-editor").fill("自己写的一条笔记：采样定理别忘了。");
  await page.getByTestId("bk-block-save").click();
  const note = page.locator('[data-testid="bk-block"][data-type="note"]');
  await expect(note).toContainText("采样定理", { timeout: 15000 });

  // 删掉（confirmDialog 确认）
  await note.hover();
  await note.getByTestId("bk-block-delete").click();
  await page.getByTestId("confirm-accept").click();
  await expect(note).toHaveCount(0, { timeout: 15000 });
});
