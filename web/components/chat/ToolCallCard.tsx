"use client";

/** 工具调用卡片（§6.1 tool_call/tool_result）：状态 + 参数/结果折叠（detail 默认折叠）。
 *
 * MCP 工具额外两样：线上名 `mcp_<服务器>_<工具>` 是给模型看的，人看的是 detail 里的
 * `mcp:<服务器>:<工具>`（§7.17）；执行中还在滚进度（tool_progress），所以这儿显示的是
 * 进度而不是「执行中」四个字——等一个远端搜索的时候，这句是唯一能证明它没卡死的东西。
 *
 * 子代理咨询（§7.12）另有一份实时活动流：CLI 在干什么由 `subagent_event` 一帧帧推来，
 * 挂在展开区（见 SubagentActivity），摘要行也借进度那行显示最新一步。 */

import { memo, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Wrench, X } from "lucide-react";
import SubagentActivity from "@/components/chat/SubagentActivity";
import { useI18n } from "@/hooks/useI18n";
import { argsLine } from "@/lib/agents";
import type { UiToolCall } from "@/hooks/useChat";

function ToolCallCard({ call }: { call: UiToolCall }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const running = call.ok === null;
  const imageDataUri = call.detail?.image_data_uri;
  const imageUrl = call.detail?.image_url;
  // MCP 适配器报的展示名（`mcp:<服务器>:<工具>`）：有就用它，线上名缩进详情里
  const display = typeof call.detail?.mcp_display === "string" ? call.detail.mcp_display : "";
  const lastStep = call.activity.length > 0 ? call.activity[call.activity.length - 1] : null;
  // 子代理一开口就替用户展开一次（实时活动是这张卡的全部看点，默认折叠等于没做），
  // 但只自动展开一次：用户收起来了就尊重这个动作，后续帧不再弹开。
  const autoOpened = useRef(false);
  useEffect(() => {
    if (!autoOpened.current && call.activity.length > 0) {
      autoOpened.current = true;
      setOpen(true);
    }
  }, [call.activity.length]);
  return (
    // w-full 不能省：外层是 items-start 的列 flex，卡片宽度会被内容撑成 max-content，
    // 摘要一行不换行 → 卡片比聊天区还宽，整页就横着出一根滚动条
    <div
      // 卡片自己也挂个 testid：一个会话里同一件工具会有好几张卡，E2E 要按名字+最后一张
      // 定位当前这张（活动流、摘要都在卡里，靠全局 testid 会撞上前面几轮的卡）
      data-testid="tool-card"
      data-name={call.name}
      data-call-id={call.call_id}
      className="mb-2 w-full overflow-hidden rounded-lg border border-border/70 bg-surface text-sm"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 transition-colors hover:bg-accent/40"
      >
        {running ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
        ) : call.ok ? (
          <Check className="h-3.5 w-3.5 text-success" />
        ) : (
          <X className="h-3.5 w-3.5 text-danger" />
        )}
        <Wrench className="h-3.5 w-3.5 text-muted" />
        <span className="font-mono text-xs font-medium">{display || call.name}</span>
        {!running && (
          <span className={`text-xs ${call.ok ? "text-success" : "text-danger"}`}>
            {call.ok ? t("chat.toolOk") : t("chat.toolFailed")}
          </span>
        )}
        {running && call.progress && (
          <span
            data-testid="tool-progress"
            className="min-w-0 flex-1 truncate text-left text-xs text-muted"
          >
            {t("chat.toolProgress")}
            {" · "}
            {call.progress}
          </span>
        )}
        {running && !call.progress && lastStep && (
          // 子代理没走 tool_progress：摘要行借最新那一步当「正在干什么」
          <span
            data-testid="subagent-step"
            className="min-w-0 flex-1 truncate text-left text-xs text-muted"
          >
            {lastStep.kind === "tool" ? lastStep.name : lastStep.text}
            {lastStep.kind === "tool" && argsLine(lastStep.args)
              ? ` ${argsLine(lastStep.args)}`
              : ""}
          </span>
        )}
        {!running && call.summary && (
          <span className="min-w-0 flex-1 truncate text-left text-muted">{call.summary}</span>
        )}
        <ChevronDown
          className={`ml-auto h-3.5 w-3.5 text-muted transition-transform ${
            open ? "" : "-rotate-90"
          }`}
        />
      </button>
      {open && (
        <div className="space-y-1 border-t border-border/70 px-3 py-2">
          {Boolean(imageDataUri) && (
            // eslint-disable-next-line @next/next/no-img-element -- 生成图是 data URI，next/image 优化不适用
            <img
              src={String(imageDataUri)}
              alt={call.name}
              className="my-1 max-h-72 rounded-lg border border-border"
            />
          )}
          {Boolean(imageUrl) && (
            <a
              href={String(imageUrl)}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-primary underline"
            >
              {String(imageUrl)}
            </a>
          )}
          {display && call.name !== display && (
            // 模型看到的名字（mcp_<服务器>_<工具>）留在这儿：排障时空一格也对不上账
            <p className="font-mono text-[11px] text-muted">{call.name}</p>
          )}
          {call.activity.length > 0 && <SubagentActivity lines={call.activity} running={running} />}
          <div className="text-xs text-muted">
            <span className="font-medium">{t("chat.toolArgs")}</span>
            <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono">
              {JSON.stringify(call.args, null, 2)}
            </pre>
          </div>
          {call.summary && (
            <div className="text-xs text-muted">
              <span className="font-medium">{t("chat.toolResult")}</span>
              <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono">
                {call.summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// call 对象只在对应工具事件里换引用：正文/思考 delta 时不重渲
export default memo(ToolCallCard);
