"use client";

/** 消息列表（§7.21）：历史消息 + 进行中回合 + 右缘会话进度条。
 *
 * 滚动策略（2026-09-28 改）：新回合开始时把**回复的开头**对到视口顶，之后一律不跟——
 * 正文再长也不把视图往底下拽，读的人自己滚。（原先逐帧追底，回复的开头总被顶走。）
 * 两处例外：换会话落到最新一条；`ask_user` 的卡出现时要人点，必须拉到看得见。
 *
 * 「对到视口顶」欠着一笔物理账（2026-10-03 补）：回合比一屏短时，它下方没有足够
 * 的滚动余量，目标 scrollTop 会被浏览器夹住、回复开头永远到不了顶。所以列表尾巴
 * 常驻一块透明占位（`writeTailSpacer`），高度 = 视口高 - 留白 - 回合顶以下的内容高：
 * 目标恰好可达，内容长过一屏后占位自动归零。回合终局时占位**不缩**（一缩
 * scrollHeight 骤减、浏览器钳位，界面会跳），直到换会话/下一回合接手。
 *
 * 进度条（2026-10-01 加）：点刻度就滚到那一轮并闪一下。它挂在外壳里、滚动容器的
 * 兄弟位——不随内容滚、不被裁；外壳只是包一层 flex，尺寸契约跟原来一样。
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useChatStore } from "@/hooks/useChat";
import { TURN_TOP_GAP_PX, tailSpacerHeight } from "@/lib/chat-scroll";
import { EMPTY_REFS } from "@/lib/refs";
import { FLASH_MS, buildQuestionEntries, jumpTargetTop } from "@/lib/chat-rail";
import ActiveTurnView from "./ActiveTurnView";
import AssistantMessage from "./AssistantMessage";
import EmptyState from "./EmptyState";
import QuestionRail from "./QuestionRail";
import UserMessage from "./UserMessage";

interface FlashTarget {
  id: string;
  /** 递增序号：同一个气泡连点两次也能重放闪烁动画 */
  seq: number;
}

export default function MessageList() {
  const messages = useChatStore((s) => s.messages);
  // 细粒度订阅（2026-10-03）：流式 delta 换的是 active 整对象，这里只吃原始值——
  // 每个 delta 不再把整个列表（连同全部历史消息）重渲一遍
  const turnId = useChatStore((s) => s.active?.turnId ?? "");
  const askId = useChatStore((s) => s.active?.askUser?.ask_id ?? "");
  const hasActive = useChatStore((s) => s.active !== null);
  const sessionId = useChatStore((s) => s.sessionId);
  const containerRef = useRef<HTMLDivElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  const turnRef = useRef<HTMLDivElement>(null);
  const spacerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const flashTimer = useRef<number | null>(null);
  const [flash, setFlash] = useState<FlashTarget | null>(null);
  // 占位状态（都走 ref，不触发渲染）：现在生效吗、钉住的回合顶在内容坐标的哪里
  const tailPinnedRef = useRef(false);
  const anchorOffsetRef = useRef<number | null>(null);
  // 上一次看到的是哪个会话：换会话时占位清零（刚进会话不该带着一屏空白）
  const pinnedSessionRef = useRef(sessionId);
  // 上一提交有没有进行中回合：终局那一提交要「盲写」占位（见 effect 里的注释）
  const prevTurnIdRef = useRef("");

  const showEmpty = !sessionId && messages.length === 0 && !hasActive;

  /** 尾巴占位：把「回合顶 - 留白」的滚动目标补到物理可达（细节见文件头注释）。
   *  直接改 DOM 高度，不走 React 渲染。锚点（回合顶）在回合进行中每帧现读；
   *  回合结束后沿用最后一帧的位置——新回复落进历史消息区就是落在原地。 */
  const writeTailSpacer = useCallback(() => {
    const container = containerRef.current;
    const spacer = spacerRef.current;
    const column = columnRef.current;
    if (!container || !spacer || !column) {
      return;
    }
    const turn = turnRef.current;
    if (turn) {
      anchorOffsetRef.current =
        turn.getBoundingClientRect().top -
        container.getBoundingClientRect().top +
        container.scrollTop;
    }
    const anchor = anchorOffsetRef.current;
    const current = spacer.getBoundingClientRect().height;
    // 锚点以下的内容高用**列**的实际高度算，不能用 container.scrollHeight：内容
    // 不满一屏时 scrollHeight 被钳到 clientHeight，减出来虚高 → 占位算小 → 反复
    // 追不上（2026-10-03 e2e 探针抓到的 +76px/轮的反馈环，且滚动目标当时不可达）
    const wanted =
      tailPinnedRef.current && anchor !== null
        ? Math.round(
            tailSpacerHeight(
              container.clientHeight,
              column.getBoundingClientRect().height - current - anchor
            )
          )
        : 0;
    if (Math.abs(wanted - current) >= 1) {
      spacer.style.height = `${wanted}px`;
    }
  }, []);

  // 回合开始钉住占位；换会话整块清零
  useLayoutEffect(() => {
    if (pinnedSessionRef.current !== sessionId) {
      pinnedSessionRef.current = sessionId;
      tailPinnedRef.current = false;
      anchorOffsetRef.current = null;
    }
    // 终局那一提交：回合块从 DOM 里消失（active 置 null 与历史消息刷新之间还有一
    // 提交），scrollHeight 会瞬缩，浏览器趁机把 scrollTop 钳到 0——实测 done 时视口
    // 直接弹回顶部。在**任何布局读数之前**盲写一块足够大的占位顶住这一瞬，同一次
    // 提交里紧接着的 writeTailSpacer 会量回准确值；中间态不上屏。
    if (prevTurnIdRef.current && !turnId && tailPinnedRef.current) {
      const spacer = spacerRef.current;
      if (spacer) {
        spacer.style.height = "9999px";
      }
    }
    prevTurnIdRef.current = turnId;
    if (turnId) {
      tailPinnedRef.current = true;
    }
    writeTailSpacer();
  }, [turnId, sessionId, writeTailSpacer]);

  // 内容长高 / 视口变化 → 重算占位。观察列与容器，绝不观察占位自身（自己触发自己）
  useLayoutEffect(() => {
    if (showEmpty) {
      return;
    }
    const observer = new ResizeObserver(() => writeTailSpacer());
    if (columnRef.current) {
      observer.observe(columnRef.current);
    }
    if (containerRef.current) {
      observer.observe(containerRef.current);
    }
    return () => observer.disconnect();
  }, [showEmpty, writeTailSpacer]);

  // 刻度条目（进度条与 ordinal 锚点都从这来）：一问一格，扁平化后为空的用户消息不占格
  const entries = useMemo(
    () => buildQuestionEntries(messages.map(({ id, role, content }) => ({ id, role, content }))),
    [messages]
  );
  const ordinalById = useMemo(() => {
    const map = new Map<string, number>();
    entries.forEach((entry) => map.set(entry.id, entry.ordinal));
    return map;
  }, [entries]);

  /** 点进度条刻度：滚到那轮头顶 + 闪一下（滚动距离交给浏览器夹，短的会话会贴底停住） */
  const jumpTo = useCallback((messageId: string) => {
    const container = containerRef.current;
    const target = container?.querySelector<HTMLElement>(
      `[data-message-id="${CSS.escape(messageId)}"]`
    );
    if (!container || !target) {
      return;
    }
    const top = jumpTargetTop(
      target.getBoundingClientRect().top,
      container.getBoundingClientRect().top,
      container.scrollTop
    );
    const maxTop = container.scrollHeight - container.clientHeight;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    container.scrollTo({
      top: Math.max(0, Math.min(top, maxTop)),
      behavior: reduceMotion ? "auto" : "smooth",
    });
    setFlash((current) => ({ id: messageId, seq: (current?.seq ?? 0) + 1 }));
    if (flashTimer.current !== null) {
      window.clearTimeout(flashTimer.current);
    }
    flashTimer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
  }, []);

  useEffect(
    () => () => {
      if (flashTimer.current !== null) {
        window.clearTimeout(flashTimer.current);
      }
    },
    []
  );

  // 换会话（历史装载）：落到最新一条
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [sessionId]);

  /** 视口顶到「进行中回合」顶部的距离（占位已在前面垫好，目标必然可达） */
  const scrollToTurnTop = useCallback(() => {
    const container = containerRef.current;
    const turn = turnRef.current;
    if (!container || !turn) {
      return;
    }
    const offset = turn.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTop += offset - TURN_TOP_GAP_PX;
  }, []);

  // 新回合：回复的开头对到视口顶，后面整屏都留给流式正文。
  // layout 阶段同步滚，随提交一起上屏，不闪中间帧（占位的 layout effect 声明在前面，
  // 同一次提交里先垫后滚——顺序不能反）
  useLayoutEffect(() => {
    if (turnId) {
      scrollToTurnTop();
    }
  }, [turnId, scrollToTurnTop]);

  // ask_user 的卡：整张都露着就不动（别抢用户正在看的滚动位置），没露全拉到顶
  useEffect(() => {
    if (!askId) {
      return;
    }
    const container = containerRef.current;
    // 卡片在 ActiveTurnView 里挂着，为一张临时卡把 ref 一路传下去不划算——按 testid 取
    const card = container?.querySelector<HTMLElement>('[data-testid="ask-card"]') ?? null;
    if (!container || !card) {
      return;
    }
    const box = container.getBoundingClientRect();
    const rect = card.getBoundingClientRect();
    if (rect.top >= box.top && rect.bottom <= box.bottom) {
      return;
    }
    card.scrollIntoView({ block: "start" });
  }, [askId]);

  if (showEmpty) {
    return <EmptyState />;
  }

  return (
    <div ref={wrapperRef} className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={containerRef}
        data-testid="message-scroll"
        className="no-scrollbar overflow-anchor-none min-h-0 flex-1 overflow-y-auto"
      >
        <div ref={columnRef} className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-6">
          {messages.map((message) =>
            message.role === "user" ? (
              <UserMessage
                key={message.id}
                messageId={message.id}
                ordinal={ordinalById.get(message.id) ?? null}
                flashSeq={flash?.id === message.id ? flash.seq : null}
                content={message.content}
                refs={message.metadata.refs ?? EMPTY_REFS}
              />
            ) : (
              <AssistantMessage key={message.id} message={message} />
            )
          )}
          {hasActive && (
            <div ref={turnRef} data-testid="active-turn">
              <ActiveTurnView />
            </div>
          )}
          {/* 尾巴占位：让「回复开头对到视口顶」物理可达；终局后不缩，换会话清零 */}
          <div ref={spacerRef} aria-hidden className="shrink-0" style={{ height: 0 }} />
          <div ref={bottomRef} />
        </div>
      </div>
      <QuestionRail
        entries={entries}
        wrapperRef={wrapperRef}
        containerRef={containerRef}
        columnRef={columnRef}
        onJump={jumpTo}
      />
    </div>
  );
}
