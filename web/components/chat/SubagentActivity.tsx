"use client";

/** 子代理活动流（§7.12）：在 `consult_subagent` 那张工具卡的展开区里，逐行看它是怎么干的。
 *
 * 为什么做成折叠区里的流、而不是独立的抽屉：咨询是**一次工具调用**，用户的心智模型是
 * 「我调了它，它跑了什么」——把过程挂在调用下面，跟工具卡的结果是一处；开个悬浮窗反而
 * 要用户自己把两边对上号（这也是用户拍板的方案）。
 *
 * 渲染规则很薄，因为服务端已经做了重的活：
 * - `block`/`thinking` 是**当前全文快照**（同 `block_id` 覆盖写），这里不再拼、不再封顶；
 * - `tool` 是「调用了什么」（参数已被服务端截断成一行摘要）；
 * - `tool_result` 是工具回话（大输出服务端只留头尾）；
 * - `status` 是 CLI 的零碎输出（非 JSON 行、stderr、会话状态），当背景信息看一眼。
 *
 * 只在运行中自动滚到底（且用户没自己往上翻），跑完就停下——不然用户想看中间某段，
 * 会被一直拽回去。 */

import { useEffect, useRef } from "react";
import { Bot, ChevronRight, Loader2, Terminal } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { argsLine } from "@/lib/agents";
import type { UiSubagentLine } from "@/hooks/useChat";

export default function SubagentActivity({
  lines,
  running,
}: {
  lines: UiSubagentLine[];
  running: boolean;
}) {
  const { t } = useI18n();
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const box = boxRef.current;
    if (!box || !running) {
      return;
    }
    // 用户自己往上翻了就先别抢（48px 容差：滚轮惯性/四舍五入都算「在底部」）
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
    if (atBottom) {
      box.scrollTop = box.scrollHeight;
    }
  }, [lines, running]);

  return (
    <div data-testid="subagent-activity" className="text-xs">
      <div className="flex items-center gap-1.5 text-muted">
        <Bot className="h-3.5 w-3.5" />
        <span className="font-medium">{t("chat.subagentActivity")}</span>
        <span data-testid="subagent-activity-count" className="text-[10px]">
          {t("chat.subagentLines", { n: String(lines.length) })}
        </span>
        {running && (
          <span
            data-testid="subagent-live"
            className="ml-auto flex items-center gap-1 text-primary"
          >
            <Loader2 className="h-3 w-3 animate-spin" />
            {t("chat.subagentRunning")}
          </span>
        )}
      </div>
      <div ref={boxRef} className="mt-1 max-h-64 space-y-1 overflow-y-auto pr-1">
        {lines.map((line, index) => (
          <ActivityLine key={`${line.block_id}-${index}`} line={line} />
        ))}
      </div>
    </div>
  );
}

function ActivityLine({ line }: { line: UiSubagentLine }) {
  const { t } = useI18n();
  if (line.kind === "tool") {
    const args = argsLine(line.args);
    return (
      <div data-testid="subagent-line-tool" className="flex items-start gap-1.5">
        <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-primary" />
        <span className="min-w-0 break-all font-mono text-[11px]">
          <span className="font-medium">{line.name || t("chat.subagentTool")}</span>
          {args && <span className="text-muted"> {args}</span>}
        </span>
      </div>
    );
  }
  if (line.kind === "tool_result") {
    return (
      <div
        data-testid="subagent-line-result"
        data-ok={line.ok === false ? "false" : "true"}
        className="flex items-start gap-1.5"
      >
        <Terminal
          className={`mt-0.5 h-3 w-3 shrink-0 ${line.ok === false ? "text-danger" : "text-muted"}`}
        />
        <pre
          className={`min-w-0 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-accent/40 p-1.5 font-mono text-[11px] ${
            line.ok === false ? "text-danger" : "text-muted"
          }`}
        >
          {line.text || t("chat.subagentResultEmpty")}
        </pre>
      </div>
    );
  }
  if (line.kind === "thinking") {
    return (
      <p
        data-testid="subagent-line-thinking"
        className="whitespace-pre-wrap break-words border-l-2 border-border pl-2 text-[11px] italic text-muted"
      >
        {line.text}
      </p>
    );
  }
  if (line.kind === "status") {
    return (
      <p
        data-testid="subagent-line-status"
        className="whitespace-pre-wrap break-words font-mono text-[11px] text-muted"
      >
        {line.text}
      </p>
    );
  }
  // block：普通正文（服务端发的是这一块的当前全文）
  return (
    <p data-testid="subagent-line-text" className="whitespace-pre-wrap break-words text-[11px]">
      {line.text}
    </p>
  );
}
