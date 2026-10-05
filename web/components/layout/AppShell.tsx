"use client";

/** 工作台布局骨架：左侧边栏 + 右侧页头与主区。 */

import { useEffect, useRef, useState } from "react";
import Header from "./Header";
import Sidebar from "./Sidebar";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useI18n } from "@/hooks/useI18n";
import { useIsNarrow } from "@/hooks/useIsNarrow";

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  const [navOpen, setNavOpen] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const narrow = useIsNarrow();
  // 只有「窄屏 + 打开」才是覆盖态抽屉：桌面端侧栏常驻，圈焦点/对话框语义都不适用
  const drawerMode = navOpen && narrow;
  useFocusTrap(drawerRef, drawerMode);

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
      {/* 键盘用户跳过常驻侧栏，直达主内容 */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-surface focus:px-3 focus:py-2 focus:text-sm focus:shadow-lg"
      >
        {t("common.skipToContent")}
      </a>
      {/* 窄屏（< md）：侧栏藏进抽屉，汉堡按钮（页头）开、遮罩/选中项/Esc 关；
          桌面端（≥ md）：照旧常驻在左侧，一个像素都不动 */}
      <div
        ref={drawerRef}
        id="app-sidebar"
        data-testid="app-sidebar"
        role={drawerMode ? "dialog" : undefined}
        aria-modal={drawerMode || undefined}
        aria-label={drawerMode ? t("common.navigation") : undefined}
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
        <Header onMenu={() => setNavOpen(true)} navOpen={navOpen} />
        <main id="main-content" className="flex min-h-0 min-w-0 flex-1 flex-col">
          {children}
        </main>
      </div>
    </div>
  );
}
