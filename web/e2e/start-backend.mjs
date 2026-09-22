/** E2E 后端启动器：先同步准备独立数据目录与样例 PDF，再拉起 scripted 后端。
 *
 * 不用 playwright 的 globalSetup：它排在 webServer 之后，后端起来时
 * 网络设置还没写好（会 seed 成默认 8001 撞开发服务器）。
 */

import { execSync, spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "..", "..");
const E2E_HOME = path.resolve(__dirname, "..", ".e2e-home");
const PYTHON = path.join(REPO, ".venv", "Scripts", "python.exe");

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
    PYTHONIOENCODING: "utf-8",
  },
});
child.on("exit", (code) => process.exit(code ?? 0));
