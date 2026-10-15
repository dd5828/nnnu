/** P11 扩展三件套（§7.16 技能 / §7.17 MCP / §7.18 命令行应用）：技能页三个标签各走一遍真实通路。
 *
 * 场景：
 *  ① 技能——现场写一个技能（SKILL.md 落盘）→ 列表可见 → 停用再启用 → 删掉（零 LLM 调用）；
 *  ② MCP——手动加一台 stdio 服务器（tests/fixtures/mcp_echo_server.py）→ 连上列出三个工具
 *     → 关掉其中一个（零 LLM 调用）；
 *  ③ CLI——真目录里搜 ffmpeg（只看着，不装）→ 探测 E2E 专用那件 → 对话里 list_cli_apps +
 *     run_cli_app → 标记真落进沙箱工作区（占脚本 Turn 40-41）。
 *
 * 文件名 zzz- 打头，排在 zz-book 之后；脚本按 LLM 调用序**全局**消费，所以本文件**不能单跑**
 * （单跑会从头吃步骤，断言全错位）——跑法：`npx playwright test`（全量，与仓库既有约定一致）。
 *
 * 为什么 CLI 那条不用 ffmpeg 跑完整趟：ffmpeg 装没装全看跑测的机器（CI 的 runner 有、
 * 很多开发机没有），「装它 → 跑它 → 落文件」就断言不了。e2epy 是 start-backend.mjs 往
 * E2E 目录里追加的一件「程序 = 本次跑测用的解释器」的应用，走的是与真应用同一条路。
 */

import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const REPO = path.resolve(__dirname, "..", "..");
const E2E_HOME = path.resolve(__dirname, "..", ".e2e-home");
const WORKSPACE_FILE = path.join(E2E_HOME, "data", "user", "workspace", "e2e-cli.txt");
const MCP_FIXTURE = path.join(REPO, "tests", "fixtures", "mcp_echo_server.py");

/** 解释器：与 start-backend.mjs 同一套挑法（CI 没有 .venv，退回 PATH 上的 python3）。 */
function resolvePython(): string {
  if (process.env.NNNU_E2E_PYTHON) {
    return process.env.NNNU_E2E_PYTHON;
  }
  const venv =
    process.platform === "win32"
      ? path.join(REPO, ".venv", "Scripts", "python.exe")
      : path.join(REPO, ".venv", "bin", "python");
  if (fs.existsSync(venv)) {
    return venv;
  }
  return process.platform === "win32" ? "python" : "python3";
}

const PYTHON = resolvePython();

/** 收集主框架的 console error（§7.7 验收同款口径）。 */
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

const SKILL_NAME = "e2e-demo-skill";
const SKILL_BODY = [
  "# E2E 演示技能",
  "",
  "这条技能由 E2E 现场写出来，验证 SKILL.md 落盘、启停与删除。",
].join("\n");

test("① 技能：写一个 → 列表可见 → 停用再启用 → 删掉", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await page.goto("/skills?tab=skills");
  await page.getByTestId("sk-new").click();
  await page.getByTestId("sk-name").fill(SKILL_NAME);
  await page.getByTestId("sk-when").fill("E2E 演示时用一下");
  await page.getByTestId("sk-desc").fill("E2E 现场创建的技能，验证写盘、启停与删除。");
  await page.getByTestId("sk-body").fill(SKILL_BODY);
  await page.getByTestId("sk-save").click();

  // 新建成功自动回列表，新技能就在里头（勾选框反映后端读回来的启用状态）
  const item = page.locator(`[data-testid="sk-item"][data-name="${SKILL_NAME}"]`);
  await expect(item).toBeVisible({ timeout: 15000 });
  const toggle = page.getByTestId(`sk-toggle-${SKILL_NAME}`);
  await expect(toggle).toBeChecked();

  await toggle.click(); // 停用
  await expect(toggle).not.toBeChecked({ timeout: 10000 });
  await toggle.click(); // 再启用
  await expect(toggle).toBeChecked({ timeout: 10000 });

  // 删除要过确认弹窗：目录与记录一起清掉
  await page.getByTestId(`sk-delete-${SKILL_NAME}`).click();
  await page.getByTestId("confirm-accept").click();
  await expect(item).toHaveCount(0, { timeout: 10000 });
  expect(errors).toEqual([]);
});

test("② MCP：手动加一台 stdio 服务器 → 连上拿到工具 → 关掉其中一个", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await page.goto("/skills?tab=mcp");
  await page.getByTestId("mcp-add").click();
  await page.getByTestId("mcp-mode-manual").click();
  await page.getByTestId("mcp-field-id").fill("e2echo");
  await page.getByTestId("mcp-field-name").fill("E2E 回声");
  await page.getByTestId("mcp-field-transport-stdio").click();
  await page.getByTestId("mcp-field-command").fill(PYTHON);
  // 参数一行一个：夹具路径里带空格也切不错
  await page.getByTestId("mcp-field-args").fill(MCP_FIXTURE);
  await page.getByTestId("mcp-save").click();

  // 保存即连接（后端 reload 是等着的），卡片上直接给出状态与工具
  const card = page.locator('[data-testid="mcp-item"][data-id="e2echo"]');
  await expect(card).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId("mcp-status-e2echo")).toHaveText("已连接", { timeout: 20000 });
  for (const name of ["echo", "slow_echo", "boom"]) {
    await expect(page.getByTestId(`mcp-tool-e2echo-${name}`)).toBeVisible();
  }

  // 关掉 boom：模型下个回合就不挂它了（界面口径：勾选框 + 「工具已停用」）
  const boom = page.getByTestId("mcp-tool-e2echo-boom");
  await boom.click();
  await expect(boom).not.toBeChecked({ timeout: 10000 });
  await expect(card.getByText("工具已停用")).toBeVisible();
  expect(errors).toEqual([]);
});

test("③ CLI：目录搜得到 ffmpeg → 探测 E2E 应用 → 对话里跑它并把标记写进工作区", async ({
  page,
}) => {
  const errors = trackConsoleErrors(page);
  await page.goto("/skills?tab=cli");

  // 真目录数据过一遍：ffmpeg 的卡片、用法指南、必填参数都来自 catalogs/cli_apps.json
  await page.getByTestId("cli-search").fill("ffmpeg");
  const ffmpeg = page.getByTestId("cli-card-ffmpeg");
  await expect(ffmpeg).toBeVisible({ timeout: 15000 });
  await ffmpeg.getByTestId("cli-guide-ffmpeg").click();
  await expect(ffmpeg).toContainText("mp3"); // 指南里写着怎么抽音频
  await expect(ffmpeg).toContainText("input");
  await expect(ffmpeg).toContainText("必填");

  // 找不到的那件：探测的失败要说人话（不是「安装失败」四个字）
  await page.getByTestId("cli-search").fill("e2missing");
  await page.getByTestId("cli-install-e2missing").click();
  await expect(page.getByTestId("cli-error-e2missing")).toContainText("nnnu-e2e-no-such-program", {
    timeout: 15000,
  });
  // 刚才那次 400 是**故意踩的**：装不上的应用回 §9.1 错误信封，浏览器把这次失败本身记成一条
  // console error（这里验的是「失败说人话」，不是「永远不失败」）。先确认到这一步只多了这一条，
  // 再清空——后面主链必须一条报错都没有。
  expect(errors.filter((text) => !text.includes("400 (Bad Request)"))).toEqual([]);
  errors.length = 0;

  // E2E 专用那件：程序就是解释器，探测一下（本机自带类不下载东西，点了就探）
  await page.getByTestId("cli-search").fill("e2epy");
  await expect(page.getByTestId("cli-card-e2epy")).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("cli-status-e2epy")).toHaveText("已装好");
  await page.getByTestId("cli-install-e2epy").click();
  await expect(page.getByTestId("cli-status-e2epy")).toHaveText("已装好", { timeout: 15000 });
  await expect(page.getByTestId("cli-program-e2epy")).toContainText("python");

  // 对话里跑它（脚本 Turn 40-41）：先看清单，再写文件
  await page.goto("/");
  const box = page.locator("textarea").first();
  await box.fill("现在有哪些命令行应用能用？");
  await box.press("Enter");
  const listCard = page.getByRole("button").filter({ hasText: "list_cli_apps" }).first();
  await expect(listCard).toBeVisible({ timeout: 20000 });
  await expect(listCard).toContainText("完成", { timeout: 20000 });
  await expect(page.getByText("清单里有一件 e2epy")).toBeVisible({ timeout: 20000 });

  await box.fill("用 e2epy 跑一段 Python，把标记写进 e2e-cli.txt");
  await box.press("Enter");
  const runCard = page.getByRole("button").filter({ hasText: "run_cli_app" }).first();
  await expect(runCard).toContainText("完成", { timeout: 30000 });
  await runCard.click(); // 展开看沙箱里真跑出来的 stdout
  await expect(page.getByText("wrote e2e-cli.txt").first()).toBeVisible({ timeout: 10000 });

  // 文件真的落在沙箱工作区里——这句才是「跑通了」，模型说跑了不算
  expect(fs.readFileSync(WORKSPACE_FILE, "utf8")).toBe("cli-marker-ok");
  expect(errors).toEqual([]);
});
