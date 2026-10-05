import type { Metadata } from "next";
import ThemeScript from "@/components/ThemeScript";
import Providers from "./providers";
import "./fonts.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "nnnu",
  description: "单用户 AI 学习工作台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // lang 初值 zh：Providers 里挂载后按语言设置改写（SSR 阶段读不到 localStorage）
    <html lang="zh-CN" suppressHydrationWarning className="h-full antialiased">
      <head>
        <ThemeScript />
      </head>
      <body className="min-h-full flex flex-col bg-background text-foreground font-sans">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
