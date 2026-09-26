"use client";

/**
 * 学习路径详情路由。看板本体在组件里，这里只包一层 Suspense：
 * 它读了 useSearchParams（薄弱点深链接 ?node=<id>），Next 要求这么包。
 */

import { Suspense } from "react";
import { Loader2 } from "lucide-react";
import LearningPathDetail from "@/components/learning/LearningPathDetail";

export default function LearningPathDetailPage() {
  return (
    <Suspense
      fallback={
        <main className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </main>
      }
    >
      <LearningPathDetail />
    </Suspense>
  );
}
