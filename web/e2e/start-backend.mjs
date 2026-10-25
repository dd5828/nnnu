/** E2E 后端启动器：先同步准备独立数据目录与样例 PDF，再拉起 scripted 后端。
 *
 * 不用 playwright 的 globalSetup：它排在 webServer 之后，后端起来时
 * 网络设置还没写好（会 seed 成默认 8001 撞开发服务器）。
 */

import { execSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "..", "..");
const E2E_HOME = path.resolve(__dirname, "..", ".e2e-home");

/** 后端解释器：NNNU_E2E_PYTHON > 仓库 .venv（Windows Scripts/、类 Unix bin/）> PATH。
 *  CI 里没建 venv（依赖直接装在 runner 的 python 上），走最后一档。 */
function resolvePython() {
  if (process.env.NNNU_E2E_PYTHON) return process.env.NNNU_E2E_PYTHON;
  const venv =
    process.platform === "win32"
      ? path.join(REPO, ".venv", "Scripts", "python.exe")
      : path.join(REPO, ".venv", "bin", "python");
  if (existsSync(venv)) return venv;
  return process.platform === "win32" ? "python" : "python3";
}

const PYTHON = resolvePython();

// 1) 每次运行先清空 E2E 数据目录：scripted 步骤与回合一一对应，脏数据会导致错位
rmSync(E2E_HOME, { recursive: true, force: true });
mkdirSync(path.join(E2E_HOME, "data", "user", "settings"), { recursive: true });
// 端口 8002/3790：避开开发环境常驻的 8001/3782
writeFileSync(
  path.join(E2E_HOME, "data", "user", "settings", "network.json"),
  JSON.stringify(
    { backend_port: 8002, frontend_port: 3790, cors_origins: ["http://localhost:3790"] },
    null,
    2
  )
);
// 1b) 假密钥：模型选择器把「没配密钥」的 provider 渲染成不可点，
//     不 seed 的话 deepseek 行是否可点取决于开发机仓库根的 .env（测试得确定性）
mkdirSync(path.join(E2E_HOME, "data", "system", "user-secrets"), { recursive: true });
writeFileSync(
  path.join(E2E_HOME, "data", "system", "user-secrets", "llm.json"),
  JSON.stringify({ keys: { deepseek: "sk-e2e-stub" }, pending: {} }, null, 2)
);

// 1c) 提示词目录搬进 E2E home：后端按 <home>/prompts 找文案，找不到才回退仓库根——
//     pip 装出来的包（CI）没有那个回退路径，必须在这里给一份
cpSync(path.join(REPO, "prompts"), path.join(E2E_HOME, "prompts"), { recursive: true });

// 1d) 记忆（§7.10）：关掉自动整合 + 四个预算清零。
//     自动整合到阈值会后台调 LLM（scripted 步骤按调用序消费，凭空多一次调用
//     会让后面的场景全部错位）；预算 0 让「立即整合」成为零 LLM 通路，便于 E2E 断言。
//     budget_extract 必须为 0：extract 是第四个模式，有条目时会调 LLM 偷吃脚本步骤。
writeFileSync(
  path.join(E2E_HOME, "data", "user", "settings", "memory.json"),
  JSON.stringify(
    {
      auto_enabled: false,
      auto_threshold_turns: 20,
      budget_update: 0,
      budget_audit: 0,
      budget_dedup: 0,
      budget_extract: 0,
      update_chunk_chars: 3000,
      trace_enabled: true,
    },
    null,
    2
  )
);

// 1e) 记忆夹具：L1 轨迹（data.seq 必须等于行号，引用才解析得过）← L2 条目 ← L3 画像。
//     图节点形如 mem-xxxxxxxx；正文与引用都指向同一份示例。
const memoryDir = path.join(E2E_HOME, "data", "user", "memory");
const now = new Date();
const ym = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`; // L1 按月分片
// 条目正文单点定义：L2/L3 的 md 与 semantic.json 的哈希/样例共用，口径不会漂
const l2Fact1 = "用户在做信号处理方向的学习，常用 Python";
const l2Fact2 = "正在从直观解释过渡到采样定理";
const l3Fact = "工程落地取向的学习者，偏好先直觉后推导";
mkdirSync(path.join(memoryDir, "trace", "chat"), { recursive: true });
mkdirSync(path.join(memoryDir, "L2"), { recursive: true });
mkdirSync(path.join(memoryDir, "L3"), { recursive: true });
const l1Rows = [
  {
    ts: 1758268800,
    event: "user_message",
    data: {
      turn_id: "turn-mem-1",
      text: "我在做信号处理方向的学习，常用 Python。",
      chars: 20,
      truncated: false,
      seq: 1,
    },
  },
  {
    ts: 1758268802,
    event: "tool_call",
    data: {
      turn_id: "turn-mem-1",
      tool_name: "web_search",
      call_id: "call-mem-1",
      ok: true,
      summary: "找到 3 条入门资料",
      args_preview: '{"query": "信号处理 入门"}',
      truncated: false,
      seq: 2,
    },
  },
  {
    ts: 1758268805,
    event: "assistant_done",
    data: {
      turn_id: "turn-mem-1",
      status: "completed",
      text: "先从采样定理和傅里叶变换讲起。",
      chars: 15,
      truncated: false,
      seq: 3,
    },
  },
  {
    ts: 1758268806,
    event: "cost",
    data: {
      turn_id: "turn-mem-1",
      tokens: 1200,
      cost: 0.0031,
      per_model: { "deepseek-chat": { tokens: 1200, cost: 0.0031 } },
      seq: 4,
    },
  },
].map((row) => ({ ...row, surface: "chat", session_id: "sess-e2e-memory" }));
writeFileSync(
  path.join(memoryDir, "trace", "chat", `${ym}.jsonl`),
  `${l1Rows.map((row) => JSON.stringify(row)).join("\n")}\n`
);
writeFileSync(
  path.join(memoryDir, "L2", "chat.md"),
  [
    "# L2 · chat 分面事实",
    "",
    `- [2026-09-19] ${l2Fact1} [L1:chat/${ym}.jsonl#1] [mem-1111aaaa]`,
    `- [2026-09-19] ${l2Fact2} [L1:chat/${ym}.jsonl#3] [mem-2222bbbb]`,
    "",
  ].join("\n")
);
writeFileSync(
  path.join(memoryDir, "L3", "profile.md"),
  [
    "# L3 · 用户画像",
    "",
    `- [2026-09-19] ${l3Fact} [L2:chat#mem-1111aaaa] [mem-3333cccc]`,
    "",
  ].join("\n")
);

// 1f) 语义图谱夹具（§7.10 补做）：extract 的派生工件。source 的哈希口径必须与
//     后端 models.text_digest 一致（sha256 of " ".join(text.split())），否则图谱页
//     会一直显示 stale 徽标。用例②会改 mem-1111aaaa → 全量跑时工件变 stale，
//     所以用例⑥不断言 stale（顺序耦合）。
const memDigest = (text) =>
  "sha256:" + createHash("sha256").update(text.trim().split(/\s+/).join(" ")).digest("hex");
writeFileSync(
  path.join(memoryDir, "semantic.json"),
  JSON.stringify(
    {
      version: 1,
      updated_at: 1758300000.5,
      lang: "zh",
      source: {
        "mem-1111aaaa": memDigest(l2Fact1),
        "mem-2222bbbb": memDigest(l2Fact2),
        "mem-3333cccc": memDigest(l3Fact),
      },
      nodes: [
        {
          id: "信号处理",
          name: "信号处理",
          type: "topic",
          refs: ["mem-1111aaaa"],
          count: 1,
          samples: [l2Fact1],
        },
        {
          id: "采样定理",
          name: "采样定理",
          type: "topic",
          refs: ["mem-2222bbbb"],
          count: 1,
          samples: [l2Fact2],
        },
      ],
      edges: [
        {
          source: "信号处理",
          target: "采样定理",
          type: "正在学习",
          refs: ["mem-2222bbbb"],
          count: 1,
        },
      ],
      stats: {
        entities: 2,
        relations: 1,
        dropped_entities: 0,
        dropped_relations: 0,
        llm_calls: 1,
      },
    },
    null,
    2
  )
);

// 1g) 目录（§7.16/§7.17/§7.18）：整份搬进 E2E home，再往里追加两条 E2E 专用条目。
//     为什么要加条目：ffmpeg 这类本机不一定装，「装它 → 对话里跑它」就断言不了。
//     e2epy 的程序就是 E2E 用的解释器：探得到、装得上、跑得动、写得出文件。
//     为什么必须整份搬而不是只放 cli_apps.json：catalogs 根一旦存在，技能市场与
//     MCP 目录也都从这儿找，漏掉哪份哪块的界面就空了。
const cliCatalogPath = path.join(E2E_HOME, "catalogs", "cli_apps.json");
cpSync(path.join(REPO, "catalogs"), path.join(E2E_HOME, "catalogs"), { recursive: true });
const cliCatalog = JSON.parse(readFileSync(cliCatalogPath, "utf8"));
cliCatalog.apps.push({
  id: "e2epy",
  name: "E2E Python",
  description: {
    zh: "E2E 专用：拿解释器真跑一段 Python，验证「装上 → 对话里跑 → 落文件」这条链。",
    en: "E2E only: runs Python with the interpreter to verify install → run in chat → file lands.",
  },
  category: "testing",
  entry_point: PYTHON,
  argv_template: ["-c", "{code}"],
  params_schema: {
    type: "object",
    properties: { code: { type: "string", description: "要执行的代码 (code to run)" } },
    required: ["code"],
  },
  guide: {
    zh: "参数用法：在 code 里给一段 Python。例：print('hi')，或用 pathlib 往当前目录写文件。",
    en: "Usage: pass Python source in code. Example: print('hi'), or write a file with pathlib.",
  },
  install: { kind: "preinstalled", target: "python" },
  platform: "any",
  network: false,
});
cliCatalog.apps.push({
  id: "e2missing",
  name: "E2E Missing",
  description: {
    zh: "E2E 专用：一件永远找不到的程序，用来验失败提示。",
    en: "E2E only: a program that never exists.",
  },
  category: "testing",
  entry_point: "nnnu-e2e-no-such-program",
  argv_template: [],
  params_schema: { type: "object", properties: {} },
  guide: { zh: "用不到（它跑不起来）", en: "unused (it never runs)" },
  install: { kind: "preinstalled", target: "nnnu-e2e-no-such-program" },
  platform: "any",
});
writeFileSync(cliCatalogPath, `${JSON.stringify(cliCatalog, null, 2)}\n`);

// 1h) 子代理（§7.12）：一条指向**假** Claude Code 的连接 + 一份可以导入的历史。
//     为什么用假的：这条链要验的是「真起进程 → 逐行读 stream-json → 超时把整棵树杀干净」，
//     而真 CLI 装没装、装得好不好全看跑测的机器（开发机上就躺着一个坏装：npm 包在、
//     原生二进制没装）。假 CLI 覆盖的路径见 fixtures/fake_claude.py 的模块头。
//     `NNNU_AGENTS_ALLOW_CUSTOM_COMMAND=1` 必须开：默认关着（那是 HTTP 上的一个任意
//     代码执行面），不开就建不出指向假 CLI 的连接。
const agentFixtures = path.join(E2E_HOME, "fixtures");
const agentCwd = path.join(agentFixtures, "claude-cwd"); // 子代理的工作目录（cwd 要真存在）
const claudeProjects = path.join(agentFixtures, "claude-projects"); // 历史导入的扫描根（夹具）
const treePidFile = path.join(agentFixtures, "tree-child.pid"); // E2E:SLEEP 那趟的子进程 pid
const fakeClaude = path.join(__dirname, "fixtures", "fake_claude.py");
mkdirSync(path.join(claudeProjects, "e2e-project"), { recursive: true });
mkdirSync(agentCwd, { recursive: true });
mkdirSync(path.join(E2E_HOME, "data", "user", "agents"), { recursive: true });

// 历史夹具：形状照 ~/.claude/projects/<项目目录>/<uuid>.jsonl，每行一个事件。
// 里面混进了三样必须被跳过的东西——isSidechain（子代理内部往返）、isMeta（系统注入）、
// 一行写一半的坏 JSON——可读消息因此只有 3 条；标题走 aiTitle（老版本没有它，退回首条用户消息）。
const histStamp = (hoursAgo) =>
  new Date(Date.now() - hoursAgo * 3600 * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
const historyLines = [
  {
    type: "user",
    timestamp: histStamp(26),
    cwd: "D:/work/dsp",
    message: { role: "user", content: "昨天在折腾采样定理：为什么非得是 2 倍频？" },
  },
  {
    type: "assistant",
    timestamp: histStamp(25.9),
    cwd: "D:/work/dsp",
    message: {
      role: "assistant",
      content: [
        { type: "text", text: "两倍频不是工程习惯，是奈奎斯特的结论：低于它，高频会被折回低频。" },
        {
          type: "tool_use",
          id: "toolu_hist_1",
          name: "Read",
          input: { file_path: "dsp/notes.md" },
        },
      ],
    },
  },
  {
    type: "user",
    isSidechain: true,
    timestamp: histStamp(25.8),
    message: { role: "user", content: "（这是子代理内部的往返，不该出现在导入结果里）" },
  },
  {
    type: "assistant",
    timestamp: histStamp(25.7),
    cwd: "D:/work/dsp",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "补一句：混叠在频谱图上看得见——不想要的频率折进来了。" }],
    },
  },
  '{"type": "assistant", "message": {',
  {
    type: "assistant",
    isMeta: true,
    aiTitle: "采样定理那点事",
    timestamp: histStamp(25.6),
    message: { role: "assistant", content: "（系统提示注入，不算对话）" },
  },
];
writeFileSync(
  path.join(claudeProjects, "e2e-project", "11111111-2222-3333-4444-555555555555.jsonl"),
  `${historyLines
    .map((line) => (typeof line === "string" ? line : JSON.stringify(line)))
    .join("\n")}\n`
);

// 连接配置：一条指向假 CLI 的 claude_code 连接（界面上一进连接页就看得见它）。
// command 覆盖只在这道环境门开着时被接受；探测会真跑一次 `--version`。
const AGENT_ID = "ag-e2e0001";
writeFileSync(
  path.join(E2E_HOME, "data", "user", "agents", "connections.json"),
  `${JSON.stringify(
    {
      connections: {
        [AGENT_ID]: {
          id: AGENT_ID,
          kind: "claude_code",
          display_name: "E2E 假 Claude",
          cwd: agentCwd,
          model_override: "e2e-model",
          permission_mode: "bypassPermissions",
          command: [PYTHON, fakeClaude],
          created_at: 1758300000,
        },
      },
    },
    null,
    2
  )}\n`
);

// 2) 样例 PDF（附件引用场景用；输出到本目录 .artifacts/）
execSync(
  `"${PYTHON}" ${path.join(__dirname, "fixtures", "make_pdf.py")} ${path.join(
    __dirname,
    ".artifacts",
    "sample.pdf"
  )}`,
  { cwd: REPO, stdio: "inherit" }
);

// 3) 起 scripted 后端（NNNU_LLM_MOCK 让工厂返回脚本替身，零 API 成本；
//    嵌入也走替身，否则知识库场景会去下 95MB 的本地模型）
const child = spawn(PYTHON, ["-m", "nnnu.api.run_server"], {
  cwd: REPO,
  stdio: "inherit",
  env: {
    ...process.env,
    NNNU_HOME: E2E_HOME,
    NNNU_LLM_MOCK: "scripted",
    NNNU_LLM_SCRIPT: path.join(__dirname, "fixtures", "chat_scenarios.yaml"),
    NNNU_EMBEDDING_MOCK: "scripted",
    // 假渲染器：math_animator 场景要跑通六阶段流水线，但不装 manim/ffmpeg（P7 遗留）
    NNNU_MANIM_MOCK: "1",
    // 子代理（§7.12，见 1h 步）：允许连接里自定义启动命令（假 CLI 靠它）、
    // 历史导入的扫描根指向夹具目录（默认根是 ~/.claude/projects，夹具要排在它前面）、
    // 树杀验证的子进程 pid 文件
    NNNU_AGENTS_ALLOW_CUSTOM_COMMAND: "1",
    NNNU_AGENTS_IMPORT_ROOTS: claudeProjects,
    FAKE_CLAUDE_TREE_PID: treePidFile,
    PYTHONIOENCODING: "utf-8",
  },
});
child.on("exit", (code) => process.exit(code ?? 0));
