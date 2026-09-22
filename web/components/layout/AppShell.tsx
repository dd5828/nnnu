"use client";

/** 工作台布局骨架：左侧边栏 + 右侧页头与主区。 */

import Header from "./Header";
import Sidebar from "./Sidebar";

export default function AppShell({ children }: { children: React.ReactNode }) {
  // h-screen + 内部滚动：Chat 等工作台页面自己管滚动区，设置页页面根部加 overflow-y-auto
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Header />
        {children}
      </div>
    </div>
  );
}
