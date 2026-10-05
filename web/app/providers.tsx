"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import ConfirmDialog from "@/components/ui/ConfirmDialog";
import { useLanguageStore } from "@/i18n/language-store";

export default function Providers({ children }: { children: React.ReactNode }) {
  const lang = useLanguageStore((s) => s.lang);
  // 界面语言 → 文档语言（读屏/断词/SEO 跟着走；SSR 阶段是 layout 里的 zh-CN 初值）
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  // 组件实例级 QueryClient，避免跨请求/用户共享缓存
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 30_000, refetchOnWindowFocus: false },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <ConfirmDialog />
    </QueryClientProvider>
  );
}
