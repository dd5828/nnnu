"use client";

/** 工作台布局骨架：左侧边栏 + 右侧页头与主区。 */

import Header from "./Header";
import Sidebar from "./Sidebar";

export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-full">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header />
        {children}
      </div>
    </div>
  );
}
