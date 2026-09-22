"use client";

/**
 * 知识库详情路由。详情本体在组件里，这里只负责包一层 Suspense：
 * 它读了 useSearchParams（引用深链接 ?doc=&page=），Next 要求这么包。
 */

import { Suspense } from "react";
import { Loader2 } from "lucide-react";
import KnowledgeDetail from "@/components/knowledge/KnowledgeDetail";

export default function KnowledgeDetailPage() {
  return (
    <Suspense
      fallback={
        <main className="flex flex-1 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
        </main>
      }
    >
      <KnowledgeDetail />
    </Suspense>
  );
}
