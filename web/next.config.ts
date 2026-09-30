import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // standalone 输出：Docker 运行时只拷贝 .next/standalone + static + public（§13）
  output: "standalone",
  // 构建目录可换（默认 .next）：Next 16 的 dev 锁就在 <distDir>/dev/lock，两个 next dev
  // 共用一个目录会直接报「Another next dev server is already running」。E2E 用
  // NNNU_DIST_DIR=.next-e2e 另开一份，跑测试就不用先停 dev.py（见 playwright.config.ts）。
  distDir: process.env.NNNU_DIST_DIR || ".next",
};

export default nextConfig;
