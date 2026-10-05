import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    ".next-e2e/**", // E2E 的独立构建目录（见 next.config.ts）
    "out/**",
    "build/**",
    "public/**", // 随仓库分发的静态资产（自托管字体 / KaTeX），不参与 lint
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
