import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      // 测试文件经 @ 别名引用应用代码；npm test 在 web/ 下运行，cwd 即 web 根
      "@": path.resolve(process.cwd(), "."),
    },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
