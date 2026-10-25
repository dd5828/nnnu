/** P12 子代理（§7.12）：连接页 → 历史导入 → 咨询的实时活动流，全程走真通路。
 *
 * 场景：
 *  ① 连接——预置的那条（指向假 Claude Code）看得见、探得到；现场建一条（自定义启动命令
 *     的门要开着）、再探一次、删掉（零 LLM 调用）；
 *  ② 历史导入——扫夹具根 → 勾一条导入 → 只读浏览转录（isSidechain/isMeta/坏行都得被跳过）
 *     → 引用到对话 → 发出后消息上留引用 chip → 点 chip 落回子代理页并展开那一条（占脚本
 *     Turn 42）；
 *  ③ 咨询——五趟真起进程：完整一趟（逐块增量）、续跑（带 --resume）、老版本兜底（只有完整块
 *     + 坏行 + stderr）、非 0 退出码、超时树杀（占脚本 Turn 43-52）。
 *
 * 为什么用假 CLI（fixtures/fake_claude.py）而不是真 claude：这条链要验的是「真起进程 → 逐行
 * 读 stream-json → 超时把整棵树杀干净」，而真 CLI 装没装、装得好不好全看跑测的机器。连接的
 * cwd、启动命令、导入根都在 start-backend.mjs 的 1h 步里备好了，这儿只管用。
 *
 * 文件名 zzzz- 打头排在最后；脚本按 LLM 调用序**全局**消费，所以本文件**不能单跑**——
 * 跑法：`npx playwright test`（全量，与仓库既有约定一致）。
 */

import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const REPO = path.resolve(__dirname, "..", "..");
const E2E_HOME = path.resolve(__dirname, "..", ".e2e-home");
const FIXTURES = path.join(E2E_HOME, "fixtures");
const AGENT_CWD = path.join(FIXTURES, "claude-cwd");
const IMPORTS_DIR = path.join(E2E_HOME, "data", "user", "agents", "imports");
const TREE_PID_FILE = path.join(FIXTURES, "tree-child.pid");
const FAKE_CLAUDE = path.join(REPO, "web", "e2e", "fixtures", "fake_claude.py");
/** start-backend.mjs 1h 步写进 connections.json 的那条连接 */
const AGENT_ID = "ag-e2e0001";
const HISTORY_TITLE = "采样定理那点事";

/** 解释器：与 start-backend.mjs 同一套挑法（CI 没有 .venv，退回 PATH 上的 python3）。 */
function resolvePython(): string {
  if (process.env.NNNU_E2E_PYTHON) {
    return process.env.NNNU_E2E_PYTHON;
  }
  const venv =
    process.platform === "win32"
      ? path.join(REPO, ".venv", "Scripts", "python.exe")
      : path.join(REPO, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3";
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

/** 某一趟咨询的那张卡：按脚本里给这趟的 call_id 认（`data-call-id`，五趟各不相同）。
 *  别按「最后一张」取——新一趟的卡还没出现的那一瞬间，`.last()` 会落在上一趟的卡上，
 *  而上一趟早就「完成」了，断言当场变成空验（踩过：第三趟压根没跑起来，这里却一直拿
 *  第二趟的卡在验，红的是「正文对不上」而不是「这趟没跑」）。 */
function consultCard(page: Page, callId: string) {
  return page.locator(`[data-testid="tool-card"][data-call-id="${callId}"]`);
}

/** 五趟咨询的 call_id：来自 fixtures/chat_scenarios.yaml 的 Turn 43-52。 */
const CALL_HAPPY = "ag-happy-1";
const CALL_RESUME = "ag-resume-1";
const CALL_PLAIN = "ag-plain-1";
const CALL_FAIL = "ag-fail-1";
const CALL_SLEEP = "ag-sleep-1";

/** 问一句。发之前先等**上一个回合真的跑完**：输入区右边的按钮从「停止」变回「发送」。
 *
 *  卡片上的「完成」只说明子代理收了尾，主模型还要接着说一段、服务端还得发 done。
 *  回合没结束就按 Enter，消息照样发得出去（Composer 不看忙闲），但会被 §6.8 的同会话
 *  互斥挡回来——页面顶部弹一条红横幅，这一趟不跑，脚本里那几步 LLM 也不会被消费。 */
async function ask(page: Page, text: string) {
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible({ timeout: 30000 });
  const box = page.locator("textarea").first();
  await box.fill(text);
  await box.press("Enter");
}

/** 进程还在不在。Windows 走 tasklist（CSV 里的 PID 带引号，精确到那一列），别处发 0 号信号。 */
function isAlive(pid: number): boolean {
  if (process.platform === "win32") {
    const out = execSync(`tasklist /FI "PID eq ${pid}" /FO CSV /NH`, { encoding: "utf8" });
    return out.includes(`"${pid}"`);
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

test("① 连接：预置那条探得到，现场建一条再删掉", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await page.goto("/agents");

  // 预置的连接：已接入标记、启动命令、工作目录都来自 1h 步写的那份配置
  const seeded = page.getByTestId(`ag-card-${AGENT_ID}`);
  await expect(seeded).toBeVisible({ timeout: 15000 });
  await expect(seeded).toContainText("E2E 假 Claude");
  await expect(page.getByTestId(`ag-wired-${AGENT_ID}`)).toHaveText("已接入");
  await expect(seeded).toContainText("fake_claude.py");

  // 探测就是真跑一次 `--version`：假 CLI 报的版本号得原样出来
  await page.getByTestId(`ag-probe-${AGENT_ID}`).click();
  const probe = page.getByTestId(`ag-probe-result-${AGENT_ID}`);
  await expect(probe).toHaveAttribute("data-available", "true", { timeout: 20000 });
  await expect(probe).toContainText("1.2.3-e2e");

  // 现场建一条：kind 先切到 codex 看一眼「先只记着」的提示，再切回 claude_code 填完
  await page.getByTestId("ag-new").click();
  await page.getByTestId("ag-kind").selectOption("codex");
  await expect(page.getByTestId("ag-kind-unsupported")).toBeVisible();
  await page.getByTestId("ag-kind").selectOption("claude_code");
  await expect(page.getByTestId("ag-kind-unsupported")).toHaveCount(0);
  await page.getByTestId("ag-name").fill("E2E 现场建的");
  await page.getByTestId("ag-cwd").fill(AGENT_CWD);
  // 自定义启动命令：默认只在 NNNU_AGENTS_ALLOW_CUSTOM_COMMAND=1 时露出来（E2E 开着）。
  // 表单按空格切词，所以解释器与脚本的路径里不能有空格（仓库路径带空格的话这里会失败）
  await page.getByTestId("ag-command").fill(`${PYTHON} ${FAKE_CLAUDE}`);
  // 没勾知情就不给建（后端也会 400 挡回来）
  await expect(page.getByTestId("ag-create")).toBeDisabled();
  await page.getByTestId("ag-ack").check();
  await page.getByTestId("ag-create").click();

  // id 是后端生成的，从卡片上读回来
  const created = page.locator('[data-testid^="ag-card-"]').filter({ hasText: "E2E 现场建的" });
  await expect(created).toHaveCount(1, { timeout: 15000 });
  const createdId = ((await created.getAttribute("data-testid")) ?? "").replace("ag-card-", "");
  await expect(page.getByTestId(`ag-wired-${createdId}`)).toHaveText("已接入");
  await page.getByTestId(`ag-probe-${createdId}`).click();
  await expect(page.getByTestId(`ag-probe-result-${createdId}`)).toHaveAttribute(
    "data-available",
    "true",
    { timeout: 20000 }
  );

  // 删掉要过确认弹窗
  await page.getByTestId(`ag-delete-${createdId}`).click();
  await page.getByTestId("confirm-accept").click();
  await expect(created).toHaveCount(0, { timeout: 15000 });
  expect(errors).toEqual([]);
});

test("② 历史导入：扫 → 勾一条导入 → 看转录 → 引用到对话 → 从消息点回来", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await page.goto("/agents?tab=imports");
  await expect(page.getByTestId("ag-import-count")).toHaveText("已导入 0 条");

  // 向导：默认 3 天（夹具文件的修改时间就是刚才，天数那关按 mtime 粗筛，过得去）
  await page.getByTestId("ag-scan-toggle").click();
  await page.getByTestId("ag-days-3").click();
  await page.getByTestId("ag-scan").click();
  const candidate = page.locator('[data-testid^="ag-candidate-imp-"]');
  await expect(candidate).toHaveCount(1, { timeout: 20000 });
  const importId = ((await candidate.getAttribute("data-testid")) ?? "").replace(
    "ag-candidate-",
    ""
  );
  await expect(candidate).toContainText(HISTORY_TITLE);
  // 夹具里 isSidechain / isMeta / 坏行都不算消息，所以是 3 条
  await expect(candidate).toContainText("3 条消息");

  await page.getByTestId(`ag-candidate-pick-${importId}`).click();
  await page.getByTestId("ag-import-picked").click();
  await expect(page.getByTestId("ag-import-result")).toContainText("本次新增/更新 1 条", {
    timeout: 20000,
  });
  await expect(page.getByTestId("ag-import-count")).toHaveText("已导入 1 条");
  const transcriptFile = path.join(IMPORTS_DIR, `${importId}.jsonl`);
  expect(fs.existsSync(transcriptFile)).toBe(true);

  // 只读浏览：三条消息、工具调用压成一行标记、子代理内部往返与系统注入都不在。
  // 导入完那一条会**自动展开**（面板的既定行为：导完直接能看），所以这里不点开——
  // 点一下反而是收起来
  const transcript = page.getByTestId(`ag-transcript-${importId}`);
  await expect(transcript).toBeVisible({ timeout: 15000 });
  await expect(transcript.locator("li")).toHaveCount(3);
  await expect(transcript).toContainText("（调用了工具：Read）");
  await expect(transcript).not.toContainText("子代理内部的往返");
  await expect(transcript).not.toContainText("系统提示注入");

  // 行首点一下收起来（深链上的 `?id=` 顺手摘掉），再点开一次（这次才写进深链）
  await page.getByTestId(`ag-import-open-${importId}`).click();
  await expect(transcript).toHaveCount(0);
  await page.getByTestId(`ag-import-open-${importId}`).click();
  await expect(transcript).toBeVisible({ timeout: 15000 });
  await expect(page).toHaveURL(new RegExp(`/agents\\?tab=imports&id=${importId}`));

  // 引用到对话：挂进输入区的 chip，点侧边栏回聊天页（客户端跳转，chip 才活得过这一跳）
  await page.getByTestId(`ag-import-ref-${importId}`).click();
  await page.locator('a[href="/"]').first().click();
  await expect(page.getByTestId("ref-chip")).toContainText(HISTORY_TITLE);

  await ask(page, "我昨天问过什么？");
  await expect(page.getByText("你昨天在琢磨采样定理的 2 倍频")).toBeVisible({ timeout: 20000 });
  // 发出去之后：消息上留下引用（服务端在发送时解析并抄送了那份转录），输入区的 chip 清空
  const msgRef = page.getByTestId("msg-ref");
  await expect(msgRef).toContainText(HISTORY_TITLE);
  await expect(page.getByTestId("ref-chip")).toHaveCount(0);

  // 点引用落回子代理页的「历史导入」标签，并把那一条展开（深链 ?tab=&id=）
  await msgRef.click();
  await expect(page).toHaveURL(new RegExp(`/agents\\?tab=imports&id=${importId}`));
  await expect(page.getByTestId(`ag-transcript-${importId}`)).toBeVisible({ timeout: 15000 });

  // 删掉：库里那行与正文文件一起清；删的正巧是当前展开的那条 → 顺带收起，地址栏上的
  // `?id=` 也摘掉（不摘的话刷新还会拿着一个不存在的 id 去取正文，白挨一次 404）
  await page.getByTestId(`ag-import-delete-${importId}`).click();
  await page.getByTestId("confirm-accept").click();
  await expect(page.getByTestId(`ag-import-${importId}`)).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("ag-import-count")).toHaveText("已导入 0 条");
  await expect(page).toHaveURL(/\/agents\?tab=imports$/);
  expect(fs.existsSync(transcriptFile)).toBe(false);
  expect(errors).toEqual([]);
});

test("③ 咨询：完整一趟 → 续跑 → 老版本兜底 → 非 0 退出 → 超时树杀", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await page.goto("/");

  // 会话级选中（粘性）：不选也能跑（只有一条连接时后端自动挑），这里显式选一条，
  // 顺带验「选中值随消息下发 → 落库 → 刷新后从会话详情水合回来」这条链
  const selector = page.getByTestId("agent-selector");
  await expect(selector).toBeVisible({ timeout: 15000 });
  await selector.click();
  await page.locator(`[data-testid="agent-option"][data-value="${AGENT_ID}"]`).click();
  await expect(selector).toContainText("E2E 假 Claude");

  // ---- 第一趟：完整流程（假 CLI 逐块推正文 + 一次工具调用 + 终局 result） ----
  await ask(page, "让子代理写个快排吧。");
  const first = consultCard(page, CALL_HAPPY);
  // 活动流跑起来：卡自动展开、运行中的脉动在
  await expect(first.getByTestId("subagent-live")).toBeVisible({ timeout: 20000 });
  await expect(first.getByTestId("subagent-activity")).toBeVisible();
  // 回声出来的那几行：旗标对、prompt 走的确实是 stdin（命令行里没有任务正文）、cwd 是连接里那个
  const argvLine = first.getByTestId("subagent-line-status").filter({ hasText: "[argv]" });
  await expect(argvLine).toContainText("--output-format stream-json");
  await expect(argvLine).toContainText("--include-partial-messages");
  await expect(argvLine).toContainText("--permission-mode bypassPermissions");
  await expect(argvLine).toContainText("--model e2e-model");
  await expect(argvLine).not.toContainText("e2e-quick-sort");
  await expect(
    first.getByTestId("subagent-line-status").filter({ hasText: "[stdin]" })
  ).toContainText("e2e-quick-sort.py");
  await expect(
    first.getByTestId("subagent-line-status").filter({ hasText: "[cwd]" })
  ).toContainText("claude-cwd");
  await expect(
    first.getByTestId("subagent-line-status").filter({ hasText: "[resume]" })
  ).toContainText("none");

  await expect(first.getByText("完成", { exact: true })).toBeVisible({ timeout: 30000 });
  // 块身份相同 → 正文只该有一行（服务端发的是全文快照，前端只做覆盖写）
  await expect(first.getByTestId("subagent-line-text")).toHaveCount(1);
  await expect(first.getByTestId("subagent-line-text")).toContainText("原地分区");
  await expect(first.getByTestId("subagent-line-tool")).toContainText("Write");
  await expect(first.getByTestId("subagent-line-tool")).toContainText(
    "file_path=e2e-quick-sort.py"
  );
  await expect(first.getByTestId("subagent-line-result")).toContainText("已写入 e2e-quick-sort.py");
  await expect(first.getByTestId("subagent-step")).toHaveCount(0); // 跑完就没有「正在干什么」了
  await expect(page.getByText("子代理写完了 e2e-quick-sort.py")).toBeVisible({ timeout: 20000 });

  // 刷新：会话详情把粘性选中的连接带回来（活动流不落库，卡片上就只剩结果）
  await page.reload();
  await expect(page.getByTestId("agent-selector")).toContainText("E2E 假 Claude", {
    timeout: 15000,
  });

  // ---- 第二趟：同一个主会话再咨询 → 后端该带上 --resume <上一趟的 CLI 会话 id> ----
  await ask(page, "接着那个文件补一句注释。");
  const second = consultCard(page, CALL_RESUME);
  await expect(second.getByText("完成", { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(
    second.getByTestId("subagent-line-status").filter({ hasText: "[resume]" })
  ).toContainText("e2e-claude-session");

  // ---- 第三趟：老版本形状——没有 stream_event，只有完整块；混一行坏 JSON + 两行 stderr ----
  await ask(page, "读一下那个工程目录，说说结构。");
  const third = consultCard(page, CALL_PLAIN);
  await expect(third.getByText("完成", { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(third.getByTestId("subagent-line-text")).toHaveCount(1);
  await expect(third.getByTestId("subagent-line-text")).toContainText(
    "只有一条完整的 assistant 消息"
  );
  await expect(third.getByTestId("subagent-line-tool")).toHaveCount(0);
  // 写一半的 JSON 只该变成一条状态行，掀不翻这趟
  await expect(
    third.getByTestId("subagent-line-status").filter({ hasText: '"message": {' })
  ).toHaveCount(1);
  // 两行 stderr 也照抄成状态行（假 CLI 开头写一行、收尾再写一行）
  const stderrLines = third.getByTestId("subagent-line-status").filter({ hasText: "[stderr]" });
  await expect(stderrLines).toHaveCount(2);
  await expect(stderrLines.first()).toContainText("这是老版本");

  // ---- 第四趟：非 0 退出码——失败要说清楚，半句结论也要带回来 ----
  await ask(page, "看看那个目录能不能读。");
  const fourth = consultCard(page, CALL_FAIL);
  await expect(fourth.getByText("失败", { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(fourth).toContainText("退出码 1");
  await expect(fourth).toContainText("打不开工程目录");
  await expect(fourth.getByTestId("subagent-line-text")).toContainText("还没读完就出事了");

  // ---- 第五趟：一直不返回 → 超时中止，连它自己起的子进程一起杀掉 ----
  await ask(page, "让子代理一直跑，别停。");
  // 先看见那个孙进程的 pid 文件并断言它真活着（不然下面「已经死了」那条等于没验）
  await expect.poll(() => fs.existsSync(TREE_PID_FILE), { timeout: 20000 }).toBe(true);
  const treePid = Number(fs.readFileSync(TREE_PID_FILE, "utf8").trim());
  expect(Number.isInteger(treePid)).toBe(true);
  expect(isAlive(treePid)).toBe(true);

  const fifth = consultCard(page, CALL_SLEEP);
  await expect(fifth.getByText("失败", { exact: true })).toBeVisible({ timeout: 30000 });
  await expect(fifth).toContainText("已中止");
  await expect(
    fifth.getByTestId("subagent-line-status").filter({ hasText: "正在结束" })
  ).toBeVisible();
  // 树杀：子进程也不能留着（只杀父进程的实现下，它会继续睡 120 秒）
  await expect.poll(() => isAlive(treePid), { timeout: 15000 }).toBe(false);
  expect(errors).toEqual([]);
});
