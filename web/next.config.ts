import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // standalone 输出：Docker 运行时只拷贝 .next/standalone + static + public（§13）
  output: "standalone",
};

export default nextConfig;
