import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // standalone 输出：Docker 运行时只拷贝 .next/standalone + static + public（§13）
  output: "standalone",
  // 构建目录可换（默认 .next）：Next 16 的 dev 锁就在 <distDir>/dev/lock，两个 next dev
  // 共用一个目录会直接报「Another next dev server is already running」。E2E 用
  // NNNU_DIST_DIR=.next-e2e 另开一份，跑测试就不用先停 dev.py（见 playwright.config.ts）。
  distDir: process.env.NNNU_DIST_DIR || ".next",
  async headers() {
    return [
      {
        // KaTeX 字体走 @font-face 拉取，是 CORS 模式请求；而 HtmlViewer 的沙箱 iframe
        // 没有 allow-same-origin，对服务器来说来源是 null origin——不开跨源字体就被拦。
        // 公开的静态字体，放开 `*` 没有暴露面（JS/CSS 是经典子资源，不走 CORS）。
        source: "/vendor/katex/fonts/:path*",
        headers: [{ key: "Access-Control-Allow-Origin", value: "*" }],
      },
    ];
  },
};

export default nextConfig;
