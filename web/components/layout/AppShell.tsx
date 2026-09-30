"use client";

/** 工作台布局骨架：左侧边栏 + 右侧页头与主区。 */

import { useEffect, useState } from "react";
import Header from "./Header";
import Sidebar from "./Sidebar";

export default function AppShell({ children }: { children: React.ReactNode }) {
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    if (!navOpen) {
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setNavOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  // h-screen + 内部滚动：Chat 等工作台页面自己管滚动区，设置页页面根部加 overflow-y-auto
  return (
    <div className="flex h-screen overflow-hidden">
      {/* 窄屏（< md）：侧栏藏进抽屉，汉堡按钮（页头）开、遮罩/选中项/Esc 关；
          桌面端（≥ md）：照旧常驻在左侧，一个像素都不动 */}
      <div
        data-testid="app-sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${
          navOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <Sidebar onNavigate={() => setNavOpen(false)} />
      </div>
      {navOpen && (
        <div
          data-testid="nav-overlay"
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          onClick={() => setNavOpen(false)}
          aria-hidden
        />
      )}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Header onMenu={() => setNavOpen(true)} />
        {children}
      </div>
    </div>
  );
}
