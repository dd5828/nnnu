"use client";

/** 会话进度条（§7.21）：右缘一排刻度 = 一轮提问，悬停看预览，点一下滚过去。
 *
 * 结构：挂在转录滚动容器的**兄弟**位（`MessageList` 的 relative 外壳下），不随内容
 * 滚、不被裁。交互状态全收在本组件里：滚动算活动刻度不碰 store，不引发消息重渲。
 * （对照过上游 DeepTutor 的 TurnNavigator：只借了常量校准与「挂兄弟位」的结构判断，
 * 实现是自研的，代码零拷贝。）
 *
 * 三件事：
 * - 槽位门：ResizeObserver 量内容列右侧空出的槽位；不足 MIN_GUTTER_PX 或提问
 *   不足两条就整个不渲染（窄屏直接没有这道 UI）。
 * - 活动刻度：容器 passive scroll + rAF 合流，按「气泡过线」算当前轮。
 * - 点击：交给 MessageList 的 onJump（滚到那轮 + 闪烁高亮）。
 */

import { useEffect, useMemo, useState } from "react";
import { useI18n } from "@/hooks/useI18n";
import {
  TRACK_MAX_VH,
  TRACK_PAD_PX,
  activeIndexFromTops,
  previewTopPx,
  rowHeightPx,
  shouldShowRail,
} from "@/lib/chat-rail";
import type { QuestionEntry } from "@/lib/chat-rail";

interface QuestionRailProps {
  entries: QuestionEntry[];
  wrapperRef: React.RefObject<HTMLDivElement | null>;
  containerRef: React.RefObject<HTMLDivElement | null>;
  columnRef: React.RefObject<HTMLDivElement | null>;
  onJump: (messageId: string) => void;
}

export default function QuestionRail({
  entries,
  wrapperRef,
  containerRef,
  columnRef,
  onJump,
}: QuestionRailProps) {
  const { t } = useI18n();
  const [gutterPx, setGutterPx] = useState(0);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [trackScrollTop, setTrackScrollTop] = useState(0);

  // 内容列右边缘到外壳右边缘的差 = 这条轨的槽位；量出来不够宽就藏轨
  useEffect(() => {
    const wrapper = wrapperRef.current;
    const column = columnRef.current;
    if (!wrapper || !column) {
      return;
    }
    const measure = () => {
      setGutterPx(wrapper.getBoundingClientRect().right - column.getBoundingClientRect().right);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(wrapper);
    observer.observe(column);
    return () => observer.disconnect();
  }, [wrapperRef, columnRef]);

  // 条目 id 联起来当依赖：会话切换/回合落地（乐观 id 换真 id）才重订阅；流式增量不动它
  const entryIds = useMemo(() => entries.map((entry) => entry.id).join("\n"), [entries]);

  // 活动刻度：气泡顶边相对容器顶，过线取最后一个。scroll 走 rAF 合流，不逐帧 setState
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !entryIds) {
      return;
    }
    let raf = 0;
    const compute = () => {
      raf = 0;
      const containerTop = container.getBoundingClientRect().top;
      const tops: number[] = [];
      container
        .querySelectorAll<HTMLElement>('[data-testid="user-message"][data-ordinal]')
        .forEach((node) => tops.push(node.getBoundingClientRect().top - containerTop));
      setActiveIndex(activeIndexFromTops(tops));
    };
    const schedule = () => {
      if (!raf) {
        raf = requestAnimationFrame(compute);
      }
    };
    compute();
    container.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      container.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      if (raf) {
        cancelAnimationFrame(raf);
      }
    };
  }, [containerRef, entryIds]);

  const visible = shouldShowRail(entries.length, gutterPx);

  // 键盘：只在看得见的时候挂（可发现性跟着视觉走，不做「看不见还能按」）
  useEffect(() => {
    if (!visible) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) {
        return;
      }
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") {
        return;
      }
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)
      ) {
        return; // 正在输入框里写东西，Alt+方向键留给系统/编辑器
      }
      event.preventDefault();
      const total = entries.length;
      if (event.key === "ArrowDown") {
        const next = activeIndex + 1;
        if (next <= total - 1) {
          onJump(entries[next].id); // 已经在最后一题就不动（不做「回到实时末尾」这种发明）
        }
        return;
      }
      onJump(entries[activeIndex <= 0 ? total - 1 : activeIndex - 1].id); // ↑ 到头绕到最后一题
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [visible, activeIndex, entries, onJump]);

  if (!visible) {
    return null;
  }

  const row = rowHeightPx(entries.length);
  const hovered = hoveredIndex === null ? null : entries[hoveredIndex];

  return (
    <nav
      data-testid="question-rail"
      aria-label={t("chat.questionRail")}
      className="absolute right-1.5 top-1/2 z-20 -translate-y-1/2"
    >
      {/* 问特别多时轨内自己滚（>40 格 × 7px 也不至于顶出屏幕） */}
      <div
        onScroll={(event) => setTrackScrollTop(event.currentTarget.scrollTop)}
        className="no-scrollbar flex flex-col overflow-y-auto overscroll-contain"
        style={{ maxHeight: `${TRACK_MAX_VH * 100}vh`, padding: `${TRACK_PAD_PX}px 0` }}
      >
        {entries.map((entry, index) => {
          const isActive = index === activeIndex;
          const isHovered = index === hoveredIndex;
          return (
            <button
              key={entry.id}
              type="button"
              data-testid="question-tick"
              data-message-id={entry.id}
              data-ordinal={entry.ordinal}
              data-active={isActive ? "true" : "false"}
              aria-current={isActive ? "true" : undefined}
              aria-label={`${entry.ordinal}. ${entry.title}`}
              onClick={() => onJump(entry.id)}
              onMouseEnter={() => setHoveredIndex(index)}
              onMouseLeave={() =>
                setHoveredIndex((current) => (current === index ? null : current))
              }
              onFocus={() => setHoveredIndex(index)}
              onBlur={() => setHoveredIndex((current) => (current === index ? null : current))}
              className="flex w-9 shrink-0 items-center justify-center"
              style={{ height: `${row}px` }}
            >
              {/* 热态只变自己（加宽提亮），格高不动——指针底下的落点不漂 */}
              <span
                aria-hidden
                className={`block h-1 rounded-full transition-all motion-reduce:transition-none ${
                  isActive
                    ? "w-[18px] bg-primary"
                    : isHovered
                      ? "w-3.5 bg-foreground/80"
                      : "w-2.5 bg-muted/35"
                }`}
              />
            </button>
          );
        })}
      </div>
      {hovered && hoveredIndex !== null && (
        <div
          data-testid="question-preview"
          aria-hidden
          className="pointer-events-none absolute right-full mr-2 w-[290px] -translate-y-1/2 rounded-xl border border-border bg-surface p-3 shadow-lg"
          style={{ top: `${previewTopPx(hoveredIndex, entries.length, trackScrollTop)}px` }}
        >
          <div
            data-testid="question-preview-ordinal"
            className="text-[11px] tabular-nums text-muted"
          >
            {hovered.ordinal}/{entries.length}
          </div>
          <p data-testid="question-preview-title" className="mt-1 line-clamp-3 text-sm font-medium">
            {hovered.title}
          </p>
          {hovered.reply && (
            <p
              data-testid="question-preview-reply"
              className="mt-1 line-clamp-2 text-xs text-muted"
            >
              {hovered.reply}
            </p>
          )}
        </div>
      )}
    </nav>
  );
}
