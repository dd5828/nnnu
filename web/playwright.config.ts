import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts", // 与 vitest 默认的 *.spec.ts 区分，互不误吃
  timeout: 60_000,
  fullyParallel: false,
  workers: 1, // 后端 scripted 步骤按调用序消费，必须串行
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3790",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      // 启动器内联做数据目录/端口/样例 PDF 准备（globalSetup 排在 webServer 之后，不适用）
      command: "node e2e/start-backend.mjs",
      cwd: __dirname,
      url: "http://127.0.0.1:8002/api/v1/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "npx next dev -p 3790",
      cwd: __dirname,
      url: "http://localhost:3790",
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        NNNU_API_BASE_URL: "http://127.0.0.1:8002",
      },
    },
  ],
});
